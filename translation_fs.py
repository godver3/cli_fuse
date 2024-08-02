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

class TranslationFS(Operations):
    def __init__(self, root, db_file, backup_dir):
        self.root = root
        self.db_file = db_file
        self.backup_dir = backup_dir
        self.last_mtime = 0
        self.lock = Lock()
        self.conn = self.create_connection()
        self.cursor = self.conn.cursor()
        self.create_table()
        self.load_translations()

        self.running = True
        self.update_event = Event()
        self.update_thread = Thread(target=self.check_for_updates)
        self.backup_thread = Thread(target=self.periodic_backup)
        self.update_thread.start()
        self.backup_thread.start()

        self.db_queue = Queue()
        self.db_thread = Thread(target=self.db_worker)
        self.db_thread.start()

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
        with self.lock:
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
            with self.lock:
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
            with self.lock:
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
        with self.lock:
            if path in self.reverse_translations:
                return self.reverse_translations[path]
            parent = os.path.dirname(path)
            while parent != '/':
                if parent in self.reverse_translations:
                    original_parent = self.reverse_translations[parent]
                    return os.path.join(original_parent, os.path.relpath(path, parent))
                parent = os.path.dirname(parent)
            return path

    def access(self, path, mode):
        logging.debug(f"access called for path: {path}")
        
        # Check if it's a virtual directory
        if path in self.virtual_dirs:
            return
        
        # Check if it's a translated path
        if path in self.reverse_translations:
            path = self.reverse_translations[path]
        
        # Check if it should be hidden
        if should_hide(path, self.translations):
            raise FuseOSError(ENOENT)
        
        full_path_value = full_path(self.root, self._translate_path(path))
        if not os.access(full_path_value, mode):
            raise FuseOSError(ENOENT)

    def getattr(self, path, fh=None):
            logging.debug(f"getattr called for path: {path}")

            if path in self.virtual_dirs:
                return dict(st_mode=(S_IFDIR | 0o755), st_nlink=2,
                            st_size=0, st_ctime=time.time(), st_mtime=time.time(),
                            st_atime=time.time(), st_uid=os.getuid(), st_gid=os.getgid())

            if should_hide(path, self.translations):
                raise FuseOSError(ENOENT)

            if path in self.reverse_translations:
                path = self.reverse_translations[path]

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
            full_path_value = full_path(self.root, path)
            if os.path.isdir(full_path_value):
                dirents.extend([d for d in os.listdir(full_path_value) if not should_hide(os.path.join(path, d), self.translations)])

        dirents.extend([d.split('/')[-1] for d in self.virtual_dirs
                        if os.path.dirname(d) == path and d != path])

        for r in set(dirents):
            yield r

    def read(self, path, length, offset, fh):
        full_path_value = full_path(self.root, self._translate_path(path))
        with open(full_path_value, 'rb') as f:
            f.seek(offset)
            return f.read(length)

    def destroy(self, path):
        self.running = False
        self.update_event.set()
        self.db_queue.put((None, None, None))  # Signal to stop db_worker
        self.update_thread.join()
        self.backup_thread.join()
        self.db_thread.join()
        self.conn.close()

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
