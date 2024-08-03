import os
import sqlite3
import logging
import time
import shutil
from fuse import FuseOSError, Operations
from errno import ENOENT
from collections import defaultdict
from stat import S_IFDIR, S_IFREG
from threading import Thread, Lock, Event
from queue import Queue
from utils import full_path, should_hide, add_virtual_dirs, remove_virtual_dirs
import functools
from errno import EINVAL
from errno import ENOTSUP
from functools import lru_cache
from io import BufferedReader

class FileHandleCache:
    def __init__(self, max_handles=100):
        self.handles = {}
        self.max_handles = max_handles

    def get(self, path, mode='rb'):
        if path not in self.handles:
            if len(self.handles) >= self.max_handles:
                oldest_path = min(self.handles, key=lambda k: self.handles[k][1])
                self.close(oldest_path)
            file = open(path, mode)
            self.handles[path] = (BufferedReader(file), 0)
        self.handles[path] = (self.handles[path][0], os.path.getmtime(path))
        return self.handles[path][0]

    def close(self, path):
        if path in self.handles:
            self.handles[path][0].close()
            del self.handles[path]

    def close_all(self):
        for handle, _ in self.handles.values():
            handle.close()
        self.handles.clear()

class TranslationFS(Operations):
    def __init__(self, root, db_file, backup_dir):
        self.root = root
        self.db_file = db_file
        self.backup_dir = backup_dir
        self.last_mtime = 0
        self.fs_lock = Lock()
        self.file_handle_cache = FileHandleCache()
        self.read_buffer_size = 1024 * 1024  # 1MB buffer

        # Initialize database
        self.conn = self.create_connection()
        self.cursor = self.conn.cursor()
        self.create_table()
        self.load_translations()

        # Initialize threading components
        self.running = True
        self.update_event = Event()
        self.update_thread = Thread(target=self.check_for_updates)
        self.backup_thread = Thread(target=self.periodic_backup)
        self.db_queue = Queue()
        self.db_thread = Thread(target=self.db_worker)

        # Start threads
        self.update_thread.start()
        self.backup_thread.start()
        self.db_thread.start()

    def getxattr(self, path, name, position=0):
        logging.debug(f"getxattr called for path: {path}, name: {name}")
        translated_path = self._translate_path(path)
        full_path_value = full_path(self.root, translated_path)
        
        try:
            return os.getxattr(full_path_value, name)
        except OSError:
            return b''  # Return an empty byte string if the attribute doesn't exist

    @lru_cache(maxsize=1000)
    def _get_full_path(self, path):
        translated_path = self._translate_path(path)
        return full_path(self.root, translated_path)

    def lock(self, path, fh, cmd, lock):
        logging.debug(f"lock called for path: {path}, cmd: {cmd}")
        # For now, we'll just pretend the lock operation always succeeds
        return None

    def create_connection(self):
        return sqlite3.connect(self.db_file, check_same_thread=False)

    def create_table(self):
        self.cursor.execute('PRAGMA journal_mode=WAL')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS translations
            (original TEXT PRIMARY KEY, translated TEXT)
        ''')
        self.conn.commit()

    def load_translations(self):
        with self.fs_lock:
            self.translations = {}
            self.reverse_translations = {}
            self.dir_structure = defaultdict(set)
            self.virtual_dirs = set()

            self.cursor.execute('SELECT original, translated FROM translations')
            for orig, trans in self.cursor.fetchall():
                self.translations[orig] = trans
                self.reverse_translations[trans] = orig
                trans_dir = os.path.dirname(trans)
                self.dir_structure[trans_dir].add(os.path.basename(trans))
                add_virtual_dirs(self.virtual_dirs, trans_dir)

    def check_for_updates(self):
        while self.running:
            try:
                mtime = os.path.getmtime(self.db_file)
                if mtime > self.last_mtime:
                    logging.info("Database file changed, reloading...")
                    self.load_translations()
                    self.last_mtime = mtime
            except Exception as e:
                logging.error(f"Error checking for updates: {str(e)}")
            self.update_event.wait(5)
            self.update_event.clear()

    def periodic_backup(self):
        while self.running:
            try:
                self.backup_database()
            except Exception as e:
                logging.error(f"Error during periodic backup: {str(e)}")
            time.sleep(3600)

    def backup_database(self):
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        backup_file = os.path.join(self.backup_dir, f"translations_backup_{timestamp}.db")
        shutil.copy2(self.db_file, backup_file)
        logging.info(f"Created backup of database: {backup_file}")

        backups = sorted([f for f in os.listdir(self.backup_dir) if f.startswith("translations_backup_")])
        for old_backup in backups[:-24]:
            os.remove(os.path.join(self.backup_dir, old_backup))

    def db_worker(self):
        while self.running:
            try:
                func, args, result_queue = self.db_queue.get()
                if func is None:
                    break
                result = func(*args)
                result_queue.put(result)
            except Exception as e:
                logging.error(f"Error in db_worker: {str(e)}")
            finally:
                self.db_queue.task_done()

    def add_translation(self, original, translated):
        result_queue = Queue()
        self.db_queue.put((self._add_translation, (original, translated), result_queue))
        return result_queue.get()

    def _add_translation(self, original, translated):
        try:
            with self.fs_lock:
                self.cursor.execute('INSERT OR REPLACE INTO translations VALUES (?, ?)', (original, translated))
                self.conn.commit()

                if original in self.translations:
                    old_translated = self.translations[original]
                    self.reverse_translations.pop(old_translated, None)
                    old_trans_dir = os.path.dirname(old_translated)
                    if old_trans_dir in self.dir_structure:
                        self.dir_structure[old_trans_dir].discard(os.path.basename(old_translated))
                        if not self.dir_structure[old_trans_dir]:
                            del self.dir_structure[old_trans_dir]
                    remove_virtual_dirs(self.virtual_dirs, self.dir_structure, old_trans_dir)

                self.translations[original] = translated
                self.reverse_translations[translated] = original

                trans_dir = os.path.dirname(translated)
                self.dir_structure[trans_dir].add(os.path.basename(translated))
                add_virtual_dirs(self.virtual_dirs, trans_dir)

                self.update_event.set()
                logging.info(f"Added translation: {original} -> {translated}")
            return True
        except sqlite3.Error as e:
            logging.error(f"Error adding translation: {str(e)}")
            return False

    def remove_translation(self, original):
        result_queue = Queue()
        self.db_queue.put((self._remove_translation, (original,), result_queue))
        return result_queue.get()

    def _remove_translation(self, original):
        try:
            with self.fs_lock:
                self.cursor.execute('DELETE FROM translations WHERE original = ?', (original,))
                self.conn.commit()

                if original in self.translations:
                    translated = self.translations.pop(original)
                    self.reverse_translations.pop(translated, None)

                    trans_dir = os.path.dirname(translated)
                    if trans_dir in self.dir_structure:
                        self.dir_structure[trans_dir].discard(os.path.basename(translated))
                        if not self.dir_structure[trans_dir]:
                            del self.dir_structure[trans_dir]

                    remove_virtual_dirs(self.virtual_dirs, self.dir_structure, trans_dir)

                self.update_event.set()
                logging.info(f"Removed translation: {original}")
            return True
        except sqlite3.Error as e:
            logging.error(f"Error removing translation: {str(e)}")
            return False

    def list_translations(self):
        result_queue = Queue()
        self.db_queue.put((self._list_translations, (), result_queue))
        return result_queue.get()

    def _list_translations(self):
        try:
            self.cursor.execute('SELECT original, translated FROM translations')
            return self.cursor.fetchall()
        except sqlite3.Error as e:
            logging.error(f"Error listing translations: {str(e)}")
            return []

    def _translate_path(self, path):
            logging.debug(f"_translate_path called for path: {path}")

            with self.fs_lock:
                # Check if the path is a translated path
                if path in self.reverse_translations:
                    original = self.reverse_translations[path]
                    logging.debug(f"Path {path} is directly translated to {original}")
                    return original

                # Check if any parent directory of the path is translated
                parent = os.path.dirname(path)
                while parent != '/':
                    if parent in self.reverse_translations:
                        original_parent = self.reverse_translations[parent]
                        translated = os.path.join(original_parent, os.path.relpath(path, parent))
                        logging.debug(f"Path {path} is translated to {translated} via parent {parent}")
                        return translated
                    parent = os.path.dirname(parent)

                logging.debug(f"Path {path} is not translated")
                return path

    def access(self, path, mode):
        logging.debug(f"access called for path: {path}, mode: {mode:o}")

        # Allow access to Plex-specific files and directories
        if path.endswith(('.grab', '.plexmatch', '.plexignore')) or '/.' in path:
            return None

        if path in self.virtual_dirs:
            return None

        translated_path = self._translate_path(path)
        full_path_value = full_path(self.root, translated_path)

        if not os.path.exists(full_path_value):
            raise FuseOSError(ENOENT)

        if not os.access(full_path_value, mode):
            raise FuseOSError(EACCES)

        return None

    def getattr(self, path, fh=None):
        logging.debug(f"getattr called for path: {path}")

        # Handle Plex-specific files and directories
        if path.endswith(('.grab', '.plexmatch', '.plexignore')) or '/.' in path:
            return dict(st_mode=(S_IFREG | 0o644), st_nlink=1,
                        st_size=0, st_ctime=time.time(), st_mtime=time.time(),
                        st_atime=time.time(), st_uid=os.getuid(), st_gid=os.getgid())

        if path in self.virtual_dirs:
            return dict(st_mode=(S_IFDIR | 0o755), st_nlink=2,
                        st_size=0, st_ctime=time.time(), st_mtime=time.time(),
                        st_atime=time.time(), st_uid=os.getuid(), st_gid=os.getgid())

        translated_path = self._translate_path(path)
        full_path_value = full_path(self.root, translated_path)

        if not os.path.exists(full_path_value):
            raise FuseOSError(ENOENT)

        st = os.lstat(full_path_value)
        return dict((key, getattr(st, key)) for key in ('st_atime', 'st_ctime',
                     'st_gid', 'st_mode', 'st_mtime', 'st_nlink', 'st_size', 'st_uid'))

    def readdir(self, path, fh):
        logging.debug(f"readdir called for path: {path}")
        dirents = ['.', '..']
        if path in self.virtual_dirs:
            dirents.extend(self.dir_structure[path])
        else:
            full_path_value = full_path(self.root, self._translate_path(path))
            if os.path.isdir(full_path_value):
                dirents.extend([d for d in os.listdir(full_path_value) if not should_hide(os.path.join(path, d), self.translations)])

        # Add Plex-specific files
        dirents.extend(['.grab', '.plexmatch', '.plexignore'])

        dirents.extend([d.split('/')[-1] for d in self.virtual_dirs
                        if os.path.dirname(d) == path and d != path])

        return list(set(dirents))

    def read(self, path, size, offset, fh):
        logging.debug(f"read called for path: {path}, size: {size}, offset: {offset}")

        # Return empty content for Plex-specific files
        if path.endswith(('.grab', '.plexmatch', '.plexignore')) or '/.' in path:
            return b''

        full_path = self._get_full_path(path)
        file = self.file_handle_cache.get(full_path)

        file.seek(offset)
        return file.read(size)

    def destroy(self, path):
        self.running = False
        self.update_event.set()
        self.db_queue.put((None, None, None))  # Signal to stop db_worker
        self.update_thread.join()
        self.backup_thread.join()
        self.db_thread.join()
        self.conn.close()
        self.file_handle_cache.close_all()

    def purge_all_translations(self):
        result_queue = Queue()
        self.db_queue.put((self._purge_all_translations, (), result_queue))
        return result_queue.get()

    def _purge_all_translations(self):
        try:
            with self.fs_lock:
                self.cursor.execute('DELETE FROM translations')
                self.conn.commit()

                self.translations.clear()
                self.reverse_translations.clear()
                self.dir_structure.clear()
                self.virtual_dirs.clear()

                self.update_event.set()
                logging.info("Purged all translations")
            return True
        except sqlite3.Error as e:
            logging.error(f"Error purging all translations: {str(e)}")
            return False

    @staticmethod
    def check_db_integrity(db_file):
        try:
            conn = sqlite3.connect(db_file)
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check")
            result = cursor.fetchone()
            conn.close()
            return result[0] == 'ok'
        except sqlite3.Error as e:
            logging.error(f"Database integrity check failed: {str(e)}")
            return False

    def open(self, path, flags):
        full_path = self._get_full_path(path)
        self.file_handle_cache.get(full_path)
        return 0

    def release(self, path, fh):
        full_path = self._get_full_path(path)
        self.file_handle_cache.close(full_path)
        return 0

def fuse_error_handler(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except OSError as e:
            if e.errno == ENOTSUP:
                logging.debug(f"{func.__name__} not supported, returning empty result")
                return b''
            else:
                logging.error(f"Error in {func.__name__}: {str(e)}", exc_info=True)
                raise FuseOSError(e.errno)
        except Exception as e:
            logging.error(f"Error in {func.__name__}: {str(e)}", exc_info=True)
            raise FuseOSError(EINVAL)
    return wrapper

TranslationFS.getattr = fuse_error_handler(TranslationFS.getattr)
TranslationFS.access = fuse_error_handler(TranslationFS.access)
TranslationFS.read = fuse_error_handler(TranslationFS.read)
TranslationFS.readdir = fuse_error_handler(TranslationFS.readdir)
TranslationFS.lock = fuse_error_handler(TranslationFS.lock)
TranslationFS.getxattr = fuse_error_handler(TranslationFS.getxattr)
TranslationFS.open = fuse_error_handler(TranslationFS.open)
TranslationFS.release = fuse_error_handler(TranslationFS.release)
