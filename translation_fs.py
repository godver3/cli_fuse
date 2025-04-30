import os
import sqlite3
import logging
import time
import shutil
from fuse import FuseOSError, Operations
from errno import ENOENT, EACCES, EROFS
from collections import defaultdict
from stat import S_IFDIR, S_IFREG
from threading import Thread, RLock, Event, Lock
from queue import Queue, Empty
from utils import full_path, should_hide, add_virtual_dirs, remove_virtual_dirs
import functools
from errno import EINVAL, ENOTSUP
from functools import lru_cache
from io import BufferedReader

class FileHandleCache:
    def __init__(self, max_handles=100):
        self.handles = {}
        self.max_handles = max_handles
        self.access_times = {}

    def get(self, path, mode='rb'):
        if path not in self.handles:
            if len(self.handles) >= self.max_handles:
                lru_path = min(self.access_times, key=self.access_times.get)
                self.close(lru_path)

            try:
                physical_path = self._resolve_physical_path(path)
                if not os.path.exists(physical_path):
                    raise FuseOSError(ENOENT)
                file = open(physical_path, mode)
                self.handles[path] = BufferedReader(file)
            except OSError as e:
                logging.error(f"Error opening file {physical_path}: {e}")
                raise FuseOSError(e.errno)

        self.access_times[path] = time.time()
        return self.handles[path]

    def _resolve_physical_path(self, path):
        return path

    def close(self, path):
        if path in self.handles:
            try:
                self.handles[path].close()
            except Exception as e:
                logging.error(f"Error closing file handle for {path}: {e}")
            finally:
                del self.handles[path]
                del self.access_times[path]

    def close_all(self):
        for path in list(self.handles.keys()):
            self.close(path)
        self.handles.clear()
        self.access_times.clear()

    def invalidate(self, fuse_path):
        if fuse_path in self.handles:
            self.close(fuse_path)
            logging.debug(f"Invalidated cache for renamed path: {fuse_path}")

class TranslationFS(Operations):
    def __init__(self, root, db_file, backup_dir):
        self.root = root
        self.db_file = db_file
        self.backup_dir = backup_dir
        self.last_mtime = 0
        self.fs_lock = RLock()
        self.db_lock = Lock()
        self.file_handle_cache = FileHandleCache()
        self.read_buffer_size = 1024 * 1024

        self.conn = self.create_connection()
        self.create_table()
        self.load_translations()

        self.running = True
        self.update_event = Event()
        self.update_thread = Thread(target=self.check_for_updates)
        self.backup_thread = Thread(target=self.periodic_backup)
        self.db_queue = Queue()
        self.db_thread = Thread(target=self.db_worker)

        self.update_thread.start()
        self.backup_thread.start()
        self.db_thread.start()
        logging.info("TranslationFS initialized and background threads started.")

    def getxattr(self, path, name, position=0):
        logging.debug(f"getxattr called for path: {path}, name: {name}")
        full_p = self._get_full_path(path)
        try:
            attr_name = name.encode('utf-8') if isinstance(name, str) else name
            value = os.getxattr(full_p, attr_name)
            return value if value is not None else b''
        except OSError as e:
            if e.errno == errno.ENODATA:
                logging.debug(f"xattr '{name}' not found for {path}")
                return b''
            elif e.errno == ENOTSUP:
                logging.warning(f"getxattr not supported on underlying FS for {full_p}")
                raise FuseOSError(ENOTSUP)
            else:
                logging.error(f"getxattr error for {path} -> {full_p}, name {name}: {e}")
                raise FuseOSError(e.errno)
        except Exception as e:
            logging.exception(f"Unexpected error in getxattr for {path}")
            raise FuseOSError(EACCES)

    @lru_cache(maxsize=1000)
    def _get_full_path(self, path):
        original_path = self._translate_path(path)
        path_relative_to_root = original_path.lstrip('/')
        full = os.path.join(self.root, path_relative_to_root)
        logging.debug(f"Resolved FUSE path '{path}' to full physical path '{full}'")
        return full

    def lock(self, path, fh, cmd, lock):
        logging.debug(f"lock called for path: {path}, cmd: {cmd}")
        return None

    def create_connection(self):
        conn = sqlite3.connect(self.db_file, check_same_thread=False)
        try:
            conn.execute('PRAGMA journal_mode=WAL;')
            logging.info("Database connection created with WAL mode.")
        except sqlite3.Error as e:
            logging.error(f"Failed to set WAL mode: {e}")
        return conn

    def create_table(self):
        try:
            with self.db_lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS translations
                    (original TEXT PRIMARY KEY, translated TEXT)
                ''')
                self.conn.commit()
                cursor.close()
            logging.info("Translations table checked/created.")
        except sqlite3.Error as e:
            logging.error(f"Error creating table: {e}")

    def load_translations(self):
        logging.debug("Loading translations from database...")
        new_translations = {}
        new_reverse_translations = {}
        new_dir_structure = defaultdict(set)
        new_virtual_dirs = set()
        try:
            cursor = self.conn.cursor()
            cursor.execute('SELECT original, translated FROM translations')
            rows = cursor.fetchall()
            cursor.close()

            for orig, trans in rows:
                new_translations[orig] = trans
                new_reverse_translations[trans] = orig
                trans_dir = os.path.dirname(trans) or '/'
                new_dir_structure[trans_dir].add(os.path.basename(trans))
                add_virtual_dirs(new_virtual_dirs, trans_dir)

            with self.fs_lock:
                self.translations = new_translations
                self.reverse_translations = new_reverse_translations
                self.dir_structure = new_dir_structure
                self.virtual_dirs = new_virtual_dirs
                try:
                    self.last_mtime = os.path.getmtime(self.db_file)
                except OSError:
                    logging.warning(f"Could not get mtime for db file {self.db_file}")
                    self.last_mtime = time.time()

            logging.info(f"Loaded {len(self.translations)} translations.")
            self._get_full_path.cache_clear()

        except sqlite3.Error as e:
            logging.error(f"Error loading translations: {e}")
        except Exception as e:
            logging.exception("Unexpected error during translation loading.")

    def check_for_updates(self):
        while self.running:
            try:
                current_mtime = os.path.getmtime(self.db_file)
                if current_mtime > self.last_mtime:
                    logging.info("Database file changed, reloading translations...")
                    self.load_translations()
            except OSError as e:
                logging.error(f"Error checking db file mtime: {e}")
            except Exception as e:
                logging.exception(f"Error in check_for_updates loop")

            self.update_event.wait(5)
            if self.running:
                self.update_event.clear()

    def periodic_backup(self):
        while self.running:
            time.sleep(3600)
            if not self.running: break

            try:
                self.backup_database()
            except Exception as e:
                logging.exception(f"Error during periodic backup")

    def backup_database(self):
        if not os.path.exists(self.backup_dir):
            try:
                os.makedirs(self.backup_dir)
            except OSError as e:
                logging.error(f"Cannot create backup directory {self.backup_dir}: {e}")
                return

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        backup_file = os.path.join(self.backup_dir, f"translations_backup_{timestamp}.db")
        try:
            if not os.path.exists(self.db_file):
                logging.warning(f"Database file {self.db_file} not found for backup.")
                return

            with self.db_lock:
                shutil.copy2(self.db_file, backup_file)
            logging.info(f"Created database backup: {backup_file}")

            backups = sorted([f for f in os.listdir(self.backup_dir) if f.startswith("translations_backup_") and f.endswith(".db")])
            for old_backup in backups[:-24]:
                try:
                    os.remove(os.path.join(self.backup_dir, old_backup))
                    logging.debug(f"Removed old backup: {old_backup}")
                except OSError as e:
                    logging.error(f"Failed to remove old backup {old_backup}: {e}")

        except sqlite3.Error as e:
            logging.error(f"SQLite error during backup prep: {e}")
        except IOError as e:
            logging.error(f"IO error during backup copy to {backup_file}: {e}")
        except Exception as e:
            logging.exception(f"Unexpected error during backup")

    def db_worker(self):
        while self.running:
            try:
                func, args, result_queue = self.db_queue.get(block=True)
                if func is None:
                    logging.info("DB worker received stop signal.")
                    break
                try:
                    result = func(*args)
                    if result_queue:
                        result_queue.put(result)
                except Exception as e:
                    logging.exception(f"Error executing DB task {func.__name__}")
                    if result_queue:
                        result_queue.put(e)
            except Empty:
                continue
            except Exception as e:
                logging.exception("Error in db_worker loop itself")
            finally:
                if func is not None:
                    self.db_queue.task_done()
        logging.info("DB worker thread finished.")

    def add_translation(self, original, translated):
        result_queue = Queue()
        self.db_queue.put((self._add_translation, (original, translated), result_queue))
        return result_queue.get()

    def _add_translation(self, original, translated):
        logging.debug(f"DB Worker: Adding/updating translation: {original} -> {translated}")
        try:
            with self.fs_lock:
                existing_original = self.reverse_translations.get(translated)
                if existing_original and existing_original != original:
                    logging.warning(f"Translated path '{translated}' is already used by '{existing_original}'. Overwriting.")
                    self._remove_translation_internal(existing_original, translated)

            with self.db_lock:
                cursor = self.conn.cursor()
                cursor.execute('INSERT OR REPLACE INTO translations (original, translated) VALUES (?, ?)', (original, translated))
                self.conn.commit()
                cursor.close()

            with self.fs_lock:
                if original in self.translations:
                    self._remove_from_memory(original, self.translations[original])

                self.translations[original] = translated
                self.reverse_translations[translated] = original
                trans_dir = os.path.dirname(translated) or '/'
                self.dir_structure[trans_dir].add(os.path.basename(translated))
                add_virtual_dirs(self.virtual_dirs, trans_dir)

            self._get_full_path.cache_clear()
            self.file_handle_cache.close_all()

            self.update_event.set()
            logging.info(f"Translation added/updated: {original} -> {translated}")
            return True

        except sqlite3.Error as e:
            logging.error(f"DB Error adding translation {original} -> {translated}: {e}")
            return False
        except Exception as e:
            logging.exception(f"Unexpected error in _add_translation")
            return False

    def _remove_translation_internal(self, original, translated_path=None):
        if translated_path is None:
            translated_path = self.translations.get(original)

        if translated_path:
            logging.debug(f"Removing mapping {original} -> {translated_path} from memory")
            self.translations.pop(original, None)
            self.reverse_translations.pop(translated_path, None)

            trans_dir = os.path.dirname(translated_path) or '/'
            base_name = os.path.basename(translated_path)
            if trans_dir in self.dir_structure:
                self.dir_structure[trans_dir].discard(base_name)
                if not self.dir_structure[trans_dir]:
                    del self.dir_structure[trans_dir]
                    remove_virtual_dirs(self.virtual_dirs, self.dir_structure, trans_dir)

    def _remove_translation(self, original):
        logging.debug(f"DB Worker: Removing translation for: {original}")
        try:
            with self.fs_lock:
                translated = self.translations.get(original)

            if not translated:
                logging.warning(f"Attempted to remove non-existent translation for {original}")
                return False

            with self.db_lock:
                cursor = self.conn.cursor()
                cursor.execute('DELETE FROM translations WHERE original = ?', (original,))
                self.conn.commit()
                cursor.close()

            with self.fs_lock:
                self._remove_from_memory(original, translated)

            self._get_full_path.cache_clear()
            self.file_handle_cache.close_all()

            self.update_event.set()
            logging.info(f"Removed translation for: {original} (was {translated})")
            return True
        except sqlite3.Error as e:
            logging.error(f"DB Error removing translation {original}: {e}")
            return False
        except Exception as e:
            logging.exception(f"Unexpected error in _remove_translation")
            return False

    def _translate_path(self, fuse_path):
        if not fuse_path.startswith('/'):
            fuse_path = '/' + fuse_path
        fuse_path = os.path.normpath(fuse_path)

        logging.debug(f"_translate_path searching for: {fuse_path}")

        with self.fs_lock:
            if fuse_path in self.reverse_translations:
                original = self.reverse_translations[fuse_path]
                logging.debug(f"Path '{fuse_path}' is directly translated to '{original}'")
                return original

            parts = fuse_path.strip('/').split('/')
            current_check_path = '/'
            best_match_original = None
            best_match_fuse_parent = None

            for i, part in enumerate(parts):
                if i == 0 and current_check_path == '/':
                    current_check_path = '/' + part
                else:
                    current_check_path = os.path.join(current_check_path, part)

                if current_check_path in self.reverse_translations:
                    best_match_original = self.reverse_translations[current_check_path]
                    best_match_fuse_parent = current_check_path
                    logging.debug(f"Found parent match: FUSE '{current_check_path}' -> Original '{best_match_original}'")

            if best_match_original:
                relative_suffix = os.path.relpath(fuse_path, best_match_fuse_parent)
                if relative_suffix == '.':
                    original_path = best_match_original
                else:
                    original_path = os.path.join(best_match_original, relative_suffix)
                logging.debug(f"Path '{fuse_path}' translated via parent to '{original_path}'")
                return os.path.normpath(original_path)
            else:
                logging.debug(f"Path '{fuse_path}' is not translated, using directly.")
                return fuse_path

    def access(self, path, mode):
        logging.debug(f"access called for path: {path}, mode: {mode:o}")
        with self.fs_lock:
            is_virtual = path in self.virtual_dirs or any(path.startswith(vp + '/') for vp in self.virtual_dirs)

        if is_virtual:
            logging.debug(f"Access granted for virtual path: {path}")
            return 0

        full_p = self._get_full_path(path)
        if not os.path.exists(full_p):
            logging.warning(f"Access check failed: Path {full_p} (from {path}) does not exist.")
            raise FuseOSError(ENOENT)

        if not os.access(full_p, mode):
            logging.warning(f"Access check failed: Mode {mode:o} denied for {full_p} (from {path}).")
            raise FuseOSError(EACCES)

        logging.debug(f"Access OK for {path} -> {full_p}")
        return 0

    def getattr(self, path, fh=None):
        logging.debug(f"getattr called for path: {path}")

        with self.fs_lock:
            is_virtual = path in self.virtual_dirs or path in self.dir_structure

        if is_virtual and not self._exists_on_disk(path):
            logging.debug(f"Returning virtual directory attrs for {path}")
            now = time.time()
            return dict(st_mode=(S_IFDIR | 0o755), st_nlink=2,
                        st_size=0, st_ctime=now, st_mtime=now,
                        st_atime=now, st_uid=os.getuid(), st_gid=os.getgid())

        full_p = self._get_full_path(path)
        try:
            st = os.lstat(full_p)
            logging.debug(f"Got real attrs for {path} -> {full_p}")
            return dict((key, getattr(st, key)) for key in ('st_atime', 'st_ctime',
                         'st_gid', 'st_mode', 'st_mtime', 'st_nlink', 'st_size', 'st_uid'))
        except OSError as e:
            logging.error(f"getattr failed for {full_p} (from {path}): {e}")
            raise FuseOSError(e.errno)

    def readdir(self, path, fh):
        logging.debug(f"readdir called for path: {path}")
        # Use a set for efficient addition and uniqueness
        final_dirents_set = {'.', '..'}

        # Get the full physical path for the directory being listed
        full_p = self._get_full_path(path)
        logging.debug(f"readdir physical path: {full_p}")

        # 1. Process physical entries
        try:
            if os.path.isdir(full_p):
                physical_contents = os.listdir(full_p)
                logging.debug(f"Physical contents for {path}: {physical_contents}")

                with self.fs_lock: # Need lock to access self.translations
                    for name in physical_contents:
                        # Construct the original path relative to the root fs
                        # Use the potentially translated FUSE path 'path' to find its original base
                        original_parent_path = self._translate_path(path)
                        # Join with the physical name found from listdir
                        original_entry_path = os.path.join(original_parent_path, name)
                        original_entry_path = os.path.normpath(original_entry_path) # Normalize (e.g., remove trailing /)

                        # Check if this original path has been translated (renamed)
                        if original_entry_path not in self.translations:
                            # If not translated, add its base name to the listing
                            final_dirents_set.add(name)
                            logging.debug(f"Keeping physical entry: {name} (original: {original_entry_path})")
                        else:
                            # If it IS translated, skip adding the original name
                            logging.debug(f"Hiding physical entry: {name} (original: {original_entry_path} is translated)")

            elif not os.path.exists(full_p):
                 logging.debug(f"Physical path {full_p} not found, directory might be purely virtual.")
                 # Check if the FUSE path is purely virtual before raising ENOENT
                 # Ensure we check within the lock for consistency
                 with self.fs_lock:
                      if path not in self.dir_structure and path not in self.virtual_dirs:
                           raise FuseOSError(ENOENT)

        except OSError as e:
            # Allow ENOENT if it's potentially a virtual dir, otherwise raise
            if e.errno == ENOENT:
                 with self.fs_lock: # Ensure check is consistent
                      if path not in self.dir_structure and path not in self.virtual_dirs:
                           logging.error(f"Error reading physical directory {full_p} and not virtual: {e}")
                           raise FuseOSError(ENOENT)
                      else:
                           logging.debug(f"Ignoring ENOENT for {full_p} as {path} is virtual.")
            else:
                 logging.error(f"Error reading physical directory {full_p}: {e}")
                 raise FuseOSError(e.errno)

        # 2. Add translated entries and virtual directories that belong here
        with self.fs_lock:
            # Add translated filenames/dirnames for this specific directory level
            # These are the *target* names of translations whose original path is elsewhere
            if path in self.dir_structure:
                translated_children = self.dir_structure[path]
                logging.debug(f"Adding translated children for {path}: {translated_children}")
                final_dirents_set.update(translated_children) # Add directly to set

            # Add virtual directories that are direct children
            # e.g., path='/foo', virtual_dirs has '/foo/bar', add 'bar'
            for virt_dir in self.virtual_dirs:
                # Check if virt_dir's parent is exactly the current path
                if os.path.dirname(virt_dir) == path and virt_dir != path:
                     child_base_name = os.path.basename(virt_dir)
                     final_dirents_set.add(child_base_name)
                     logging.debug(f"Adding virtual directory child: {child_base_name}")

        logging.debug(f"Final dirents for {path}: {list(final_dirents_set)}")
        return list(final_dirents_set) # Convert set back to list for FUSE

    def read(self, path, size, offset, fh):
        logging.debug(f"read called for path: {path}, size: {size}, offset: {offset}")
        full_p = self._get_full_path(path)
        try:
            file = self.file_handle_cache.get(full_p, 'rb')
            file.seek(offset)
            data = file.read(size)
            logging.debug(f"Read {len(data)} bytes from {path} -> {full_p}")
            return data
        except FileNotFoundError:
            logging.error(f"Read error: File not found at {full_p} (from {path})")
            raise FuseOSError(ENOENT)
        except PermissionError:
            logging.error(f"Read error: Permission denied for {full_p} (from {path})")
            raise FuseOSError(EACCES)
        except IsADirectoryError:
            logging.error(f"Read error: Is a directory {full_p} (from {path})")
            raise FuseOSError(errno.EISDIR)
        except OSError as e:
            logging.error(f"Read error for {full_p} (from {path}): {e}")
            raise FuseOSError(e.errno)
        except Exception as e:
            logging.exception(f"Unexpected error during read for {path}")
            raise FuseOSError(EACCES)

    def destroy(self, path):
        logging.info("Unmounting filesystem. Stopping background threads...")
        self.running = False
        self.update_event.set()
        self.db_queue.put((None, None, None))

        self.update_thread.join(timeout=2)
        self.backup_thread.join(timeout=2)
        self.db_thread.join(timeout=5)

        if self.update_thread.is_alive(): logging.warning("Update thread did not terminate cleanly.")
        if self.backup_thread.is_alive(): logging.warning("Backup thread did not terminate cleanly.")
        if self.db_thread.is_alive(): logging.warning("DB worker thread did not terminate cleanly.")

        try:
            self.conn.close()
            logging.info("Database connection closed.")
        except Exception as e:
            logging.error(f"Error closing database connection: {e}")

        self.file_handle_cache.close_all()
        logging.info("File handle cache cleared.")
        logging.info("Filesystem destroyed.")

    def _exists_on_disk(self, fuse_path):
        full_p = self._get_full_path(fuse_path)
        return os.path.lexists(full_p)

    def _remove_from_memory(self, original, translated):
        self.translations.pop(original, None)
        self.reverse_translations.pop(translated, None)

        trans_dir = os.path.dirname(translated) or '/'
        base_name = os.path.basename(translated)
        if trans_dir in self.dir_structure:
            self.dir_structure[trans_dir].discard(base_name)
            if not self.dir_structure[trans_dir]:
                del self.dir_structure[trans_dir]
                remove_virtual_dirs(self.virtual_dirs, self.dir_structure, trans_dir)

    def open(self, path, flags):
        logging.debug(f"open called for path: {path}, flags: {flags:#o}")

        is_readonly_flag = (flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC)) == 0
        if not is_readonly_flag:
            logging.warning(f"Denied write-related open flags ({flags:#o}) for {path}")
            raise FuseOSError(EROFS)

        full_p = self._get_full_path(path)
        try:
            self.file_handle_cache.get(full_p, 'rb')
            logging.debug(f"File opened (cached) for {path} -> {full_p}")
            return 0
        except FileNotFoundError:
            logging.error(f"Open error: File not found at {full_p} (from {path})")
            raise FuseOSError(ENOENT)
        except PermissionError:
            logging.error(f"Open error: Permission denied for {full_p} (from {path})")
            raise FuseOSError(EACCES)
        except IsADirectoryError:
            logging.error(f"Open error: Is a directory {full_p} (from {path})")
            raise FuseOSError(errno.EISDIR)
        except OSError as e:
            logging.error(f"Open error for {full_p} (from {path}): {e}")
            raise FuseOSError(e.errno)
        except Exception as e:
            logging.exception(f"Unexpected error during open for {path}")
            raise FuseOSError(EACCES)

    def release(self, path, fh):
        logging.debug(f"release called for path: {path}, fh: {fh}")
        full_p = self._get_full_path(path)
        self.file_handle_cache.close(full_p)
        logging.debug(f"Released file handle for {path} -> {full_p}")
        return 0

    def _get_full_path_if_untranslated(self, fuse_path):
        """
        Helper to get the potential full physical path IF the fuse_path
        is NOT currently covered by a translation (exact or parent).
        Returns None if the path IS translated or doesn't map cleanly.
        """
        norm_fuse_path = os.path.normpath(fuse_path)
        with self.fs_lock:
            # Check direct translation
            if norm_fuse_path in self.reverse_translations:
                logging.debug(f"_get_full_path_if_untranslated: Path '{norm_fuse_path}' is directly translated.")
                return None # Path is translated

            # Check parent translation
            parts = norm_fuse_path.strip('/').split('/')
            current_check_path = '/'
            # Iterate through potential parent paths
            # Example: /a/b/c -> check /, /a, /a/b, /a/b/c
            for i, part in enumerate(parts):
                if not part: continue # Skip empty parts resulting from split('/') or normpath

                if i == 0 and norm_fuse_path.startswith('/'):
                    current_check_path = '/' + part # Handle root level
                elif current_check_path == '/':
                     current_check_path = '/' + part # Handle first part after root if root wasn't checked yet
                else:
                    current_check_path = os.path.join(current_check_path, part)

                logging.debug(f"_get_full_path_if_untranslated: Checking parent '{current_check_path}' for translation.")
                if current_check_path in self.reverse_translations:
                    logging.debug(f"_get_full_path_if_untranslated: Path '{norm_fuse_path}' is under translated parent '{current_check_path}'.")
                    return None # Path is under a translated parent

        # If no translation found, calculate potential physical path
        try:
            # Assume if it's not translated, it maps directly relative to root
            path_relative_to_root = norm_fuse_path.lstrip('/')
            full = os.path.join(self.root, path_relative_to_root)
            logging.debug(f"_get_full_path_if_untranslated: Path '{norm_fuse_path}' is untranslated, maps to '{full}'.")
            return full
        except Exception as e:
             logging.error(f"_get_full_path_if_untranslated: Error calculating physical path for '{norm_fuse_path}': {e}")
             return None

    def rename(self, old, new):
        logging.info(f"rename called: {old} -> {new}")

        # Resolve the *original* filesystem path for the FUSE path being renamed ('old')
        original_old_path = self._translate_path(old)
        full_original_old = os.path.join(self.root, original_old_path.lstrip('/'))

        if not os.path.exists(full_original_old):
            logging.error(f"Rename failed: Source '{old}' (original: '{original_old_path}') does not exist.")
            raise FuseOSError(ENOENT)

        # Normalize paths
        norm_original_old_path = os.path.normpath(original_old_path)
        norm_target_fuse_path = os.path.normpath(new)
        logging.debug(f"Rename normalized: original='{norm_original_old_path}', target='{norm_target_fuse_path}'")

        # --- Pre-checks ---
        db_op = None
        db_args = None

        # 1. Check for renaming item into itself or its descendants
        #    (e.g., mv /dir /dir/subdir or mv /file /file/invalid)
        #    Also handles renaming back to the exact original path.
        if norm_target_fuse_path == norm_original_old_path or norm_target_fuse_path.startswith(norm_original_old_path.rstrip('/') + '/'):
             # If target is the same as original, we remove the translation
             if norm_target_fuse_path == norm_original_old_path:
                 logging.info(f"Detected rename back to original path: {old} ({norm_original_old_path}) -> {new}. Removing translation.")
                 db_op = self._remove_translation
                 db_args = (norm_original_old_path,)
             else:
                 # Target is inside the original path - this is invalid for virtual renames
                 logging.error(f"Rename failed: Cannot rename '{old}' (original: '{norm_original_old_path}') into itself ('{new}').")
                 raise FuseOSError(EINVAL) # Invalid argument
        else:
            # Standard rename (not into self, not back to original)

            # 2. Check if renaming TO a path that is currently an ancestor of the original path's FUSE representation ('old')
            #    (e.g. mv /a/b/c /a ) - This is disallowed by standard 'mv'
            #    Need to use the 'old' FUSE path for this check, not the translated original path.
            norm_old_fuse_path = os.path.normpath(old)
            if norm_old_fuse_path.startswith(norm_target_fuse_path.rstrip('/') + '/'):
                 logging.error(f"Rename failed: Cannot rename '{old}' to an ancestor directory '{new}'.")
                 raise FuseOSError(EINVAL)

            # 3. Check if the target path conflicts with an existing *physical* path
            #    that isn't already managed by a translation we're about to overwrite.
            potential_physical_target = self._get_full_path_if_untranslated(norm_target_fuse_path)
            target_physically_exists = potential_physical_target and os.path.lexists(potential_physical_target) # Use lexists for symlinks

            if target_physically_exists:
                 # Target physically exists. Is it okay to overwrite?
                 # It's okay *only* if the target FUSE path is ALREADY translated
                 # (meaning we are just changing where an existing translation points).
                 with self.fs_lock:
                      is_target_fuse_path_translated = norm_target_fuse_path in self.reverse_translations
                 if not is_target_fuse_path_translated:
                      logging.error(f"Rename failed: Target '{new}' conflicts with an existing physical path '{potential_physical_target}' that is not managed by a translation.")
                      raise FuseOSError(errno.EEXIST) # Target exists and isn't virtual
                 else:
                      logging.debug(f"Target '{new}' conflicts with physical path '{potential_physical_target}', but target FUSE path is already translated. Allowing overwrite.")

            # If all checks pass, queue the add/update operation
            logging.info(f"Adding/updating translation for rename: {old} ({norm_original_old_path}) -> {new} ({norm_target_fuse_path})")
            db_op = self._add_translation
            db_args = (norm_original_old_path, norm_target_fuse_path)

        # --- Execute DB operation ---
        if db_op is None or db_args is None:
             # Should not happen if logic above is correct, but as a safeguard:
             logging.error("Rename failed: Internal logic error, no DB operation determined.")
             raise FuseOSError(EACCES) # Generic error

        result_queue = Queue()
        self.db_queue.put((db_op, db_args, result_queue))

        try:
            success_or_error = result_queue.get(timeout=10) # Increased timeout slightly

            # Check if the operation returned an error (Exception)
            if isinstance(success_or_error, Exception):
                 logging.error(f"Rename failed: DB worker returned an exception: {success_or_error}")
                 raise FuseOSError(getattr(success_or_error, 'errno', EACCES))
            # Check if the operation returned False (indicating failure or no-op)
            elif not success_or_error:
                 if db_op == self._add_translation:
                     logging.error(f"Rename failed: DB add/update operation returned False for {norm_original_old_path} -> {norm_target_fuse_path}")
                     raise FuseOSError(EACCES) # DB Error during add/update
                 else: # db_op == self._remove_translation
                      logging.info(f"Rename to original: DB remove operation returned False (likely no existing translation found), proceeding.")
                      # Still invalidate caches, as memory state might have been briefly inconsistent
                      self._get_full_path.cache_clear()
                      self.file_handle_cache.invalidate(old)
                      self.file_handle_cache.invalidate(new) # new == original here

            # Operation succeeded (True)
            else:
                logging.info(f"Rename DB operation successful for {old} -> {new}")
                # Invalidate caches after successful add or remove
                self._get_full_path.cache_clear()
                self.file_handle_cache.invalidate(old)
                self.file_handle_cache.invalidate(new)

            # Trigger update check in case external tools rely on mtime
            self.update_event.set()
            return 0 # Success

        except Empty:
            logging.error("Rename failed: DB worker timed out.")
            raise FuseOSError(EACCES) # Consider ETIMEDOUT if available/appropriate
        except FuseOSError:
            raise
        except Exception as e:
            logging.exception(f"Unexpected error handling rename result for {old} -> {new}")
            raise FuseOSError(EACCES)

    def write(self, path, data, offset, fh):
        logging.warning(f"Denied write attempt to {path}")
        raise FuseOSError(EROFS)

    def truncate(self, path, length, fh=None):
        logging.warning(f"Denied truncate attempt for {path}")
        raise FuseOSError(EROFS)

    def create(self, path, mode, fi=None):
        logging.warning(f"Denied create attempt for {path}")
        raise FuseOSError(EROFS)

    def unlink(self, path):
        logging.warning(f"Denied unlink attempt for {path}")
        raise FuseOSError(EROFS)

    def mkdir(self, path, mode):
        logging.warning(f"Denied mkdir attempt for {path}")
        raise FuseOSError(EROFS)

    def rmdir(self, path):
        logging.warning(f"Denied rmdir attempt for {path}")
        raise FuseOSError(EROFS)

    def chmod(self, path, mode):
        logging.warning(f"Denied chmod attempt for {path}")
        raise FuseOSError(EROFS)

    def chown(self, path, uid, gid):
        logging.warning(f"Denied chown attempt for {path}")
        raise FuseOSError(EROFS)

    @staticmethod
    def check_db_integrity(db_file):
        logging.info(f"Checking database integrity for {db_file}")
        if not os.path.exists(db_file):
            logging.warning("Database file not found for integrity check.")
            return True

        conn = None
        try:
            conn = sqlite3.connect(db_file, uri=True, check_same_thread=False)
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check;")
            result = cursor.fetchone()
            cursor.close()
            logging.info(f"Integrity check result: {result[0]}")
            return result[0] == 'ok'
        except sqlite3.Error as e:
            logging.error(f"Database integrity check failed: {e}")
            return False
        finally:
            if conn:
                conn.close()

def fuse_error_handler(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except FuseOSError as e:
            raise
        except OSError as e:
            if e.errno in (ENOENT, EACCES, EROFS, ENOTSUP, errno.EISDIR, errno.ENOTDIR):
                logging.debug(f"OSError in {func.__name__} -> FUSE {e.errno} ({os.strerror(e.errno)}): {e}")
            else:
                logging.error(f"OSError in {func.__name__} -> FUSE {e.errno}: {e}", exc_info=True)
            raise FuseOSError(e.errno)
        except Exception as e:
            logging.exception(f"Unexpected error in {func.__name__}")
            raise FuseOSError(EACCES)
    return wrapper

TranslationFS.access = fuse_error_handler(TranslationFS.access)
TranslationFS.getattr = fuse_error_handler(TranslationFS.getattr)
TranslationFS.readdir = fuse_error_handler(TranslationFS.readdir)
TranslationFS.read = fuse_error_handler(TranslationFS.read)
TranslationFS.getxattr = fuse_error_handler(TranslationFS.getxattr)
TranslationFS.open = fuse_error_handler(TranslationFS.open)
TranslationFS.release = fuse_error_handler(TranslationFS.release)
TranslationFS.rename = fuse_error_handler(TranslationFS.rename)
TranslationFS.write = fuse_error_handler(TranslationFS.write)
TranslationFS.truncate = fuse_error_handler(TranslationFS.truncate)
TranslationFS.create = fuse_error_handler(TranslationFS.create)
TranslationFS.unlink = fuse_error_handler(TranslationFS.unlink)
TranslationFS.mkdir = fuse_error_handler(TranslationFS.mkdir)
TranslationFS.rmdir = fuse_error_handler(TranslationFS.rmdir)
TranslationFS.chmod = fuse_error_handler(TranslationFS.chmod)
TranslationFS.chown = fuse_error_handler(TranslationFS.chown)
TranslationFS.destroy = fuse_error_handler(TranslationFS.destroy)
