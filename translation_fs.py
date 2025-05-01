import os
import sqlite3
import logging
import time
import shutil
from fuse import FuseOSError, Operations
from errno import ENOENT, EACCES, EROFS, EEXIST, ENOTDIR, EISDIR, ENOTSUP
from collections import defaultdict
from stat import S_IFDIR, S_IFREG
from threading import Thread, RLock, Event, Lock
from queue import Queue, Empty
from utils import full_path, should_hide, add_virtual_dirs, remove_virtual_dirs
import functools
from errno import EINVAL, ENOTSUP
from functools import lru_cache
from io import BufferedReader
import errno

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
        self.explicit_virtual_dirs = set()
        self.physically_empty_parents = set()

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
            attr_name_bytes = name.encode('utf-8') if isinstance(name, str) else name
            value = os.getxattr(full_p, attr_name_bytes)
            return value if value is not None else b''
        except OSError as e:
            if e.errno == errno.ENODATA:
                logging.debug(f"xattr '{name}' not found (ENODATA) for {path} -> {full_p}")
                return b''
            elif e.errno == errno.ENOTSUP:
                logging.warning(f"getxattr not supported (ENOTSUP) on underlying FS for {full_p} (attr: '{name}')")
                raise FuseOSError(errno.ENOTSUP)
            elif e.errno == errno.EIO:
                logging.error(f"getxattr failed with EIO (Input/Output Error) for {full_p} (attr: '{name}'): {e}")
                raise FuseOSError(errno.EIO)
            else:
                logging.error(f"getxattr OSError for {path} -> {full_p}, name '{name}': {e}")
                raise FuseOSError(e.errno)
        except Exception as e:
            logging.exception(f"Unexpected error in getxattr for {path}, name '{name}'")
            raise FuseOSError(errno.EACCES)

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
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS explicit_virtual_dirs
                    (path TEXT PRIMARY KEY)
                ''')
                self.conn.commit()
                cursor.close()
            logging.info("Tables 'translations' and 'explicit_virtual_dirs' checked/created.")
        except sqlite3.Error as e:
            logging.error(f"Error creating table(s): {e}")

    def load_translations(self):
        logging.debug("Loading translations and explicit virtual dirs from database...")
        new_translations = {}
        new_reverse_translations = {}
        new_dir_structure = defaultdict(set)
        new_virtual_dirs = set()
        new_explicit_virtual_dirs = set()

        try:
            cursor = self.conn.cursor()
            cursor.execute('SELECT original, translated FROM translations')
            translation_rows = cursor.fetchall()

            cursor.execute('SELECT path FROM explicit_virtual_dirs')
            explicit_rows = cursor.fetchall()
            cursor.close()

            for orig, trans in translation_rows:
                new_translations[orig] = trans
                new_reverse_translations[trans] = orig
                trans_dir = os.path.dirname(trans) or '/'
                base_name = os.path.basename(trans)
                if base_name:
                    new_dir_structure[trans_dir].add(base_name)
                add_virtual_dirs(new_virtual_dirs, trans_dir)

            for row in explicit_rows:
                explicit_path = row[0]
                new_explicit_virtual_dirs.add(explicit_path)
                new_virtual_dirs.add(explicit_path)
                add_virtual_dirs(new_virtual_dirs, explicit_path)
                parent = os.path.dirname(explicit_path) or '/'
                basename = os.path.basename(explicit_path)
                if basename:
                    if parent not in new_dir_structure:
                        new_dir_structure[parent] = set()
                    new_dir_structure[parent].add(basename)

            with self.fs_lock:
                self.translations = new_translations
                self.reverse_translations = new_reverse_translations
                self.dir_structure = new_dir_structure
                self.virtual_dirs = new_virtual_dirs
                self.explicit_virtual_dirs = new_explicit_virtual_dirs
                try:
                    self.last_mtime = os.path.getmtime(self.db_file)
                except OSError:
                    logging.warning(f"Could not get mtime for db file {self.db_file}")
                    self.last_mtime = time.time()

            logging.info(f"Loaded {len(self.translations)} translations and {len(self.explicit_virtual_dirs)} explicit virtual dirs.")
            self._get_full_path.cache_clear()

        except sqlite3.Error as e:
            logging.error(f"Error loading translations/virtual dirs: {e}")
        except Exception as e:
            logging.exception("Unexpected error during translation/virtual dir loading.")

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
            self._check_and_update_parent_emptiness(original)
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
                    remove_virtual_dirs(self.virtual_dirs, self.dir_structure, self.explicit_virtual_dirs, trans_dir)

            original_parent = os.path.dirname(original)
            if original_parent and original_parent != '/':
                try:
                    physical_parent_path = self._get_full_path(original_parent)
                    if physical_parent_path in self.physically_empty_parents:
                        logging.info(f"Translation removed for child of {physical_parent_path}. Unmarking as empty.")
                        self.physically_empty_parents.discard(physical_parent_path)
                except FuseOSError:
                    pass # Parent might not map physically anymore

    def _remove_translation(self, original):
        logging.debug(f"DB Worker: Removing translation for: {original}")
        t_start = time.time()
        try:
            with self.fs_lock:
                translated = self.translations.get(original)
            t_lock1 = time.time()

            if not translated:
                logging.warning(f"Attempted to remove non-existent translation for {original}")
                return False

            with self.db_lock:
                cursor = self.conn.cursor()
                cursor.execute('DELETE FROM translations WHERE original = ?', (original,))
                self.conn.commit()
                cursor.close()
            t_db = time.time()

            with self.fs_lock:
                self._remove_from_memory(original, translated)
            t_mem = time.time()

            self._get_full_path.cache_clear()
            self.file_handle_cache.close_all()
            t_cache = time.time()

            self.update_event.set()
            logging.info(f"Removed translation for: {original} (was {translated})")

            # Check parent emptiness *after* logging removal
            self._check_and_update_parent_emptiness(original)
            t_parent_check = time.time()

            logging.debug(f"Timing for remove {original}: "
                          f"Lock1={t_lock1-t_start:.4f}s, "
                          f"DB={t_db-t_lock1:.4f}s, "
                          f"Mem={t_mem-t_db:.4f}s, "
                          f"Cache={t_cache-t_mem:.4f}s, "
                          f"ParentCheck={t_parent_check-t_cache:.4f}s, "
                          f"Total={t_parent_check-t_start:.4f}s")
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
        norm_path = os.path.normpath(path)

        with self.fs_lock:
            # Priority 1: Is it an original path that is currently translated elsewhere? If yes, hide it.
            # This is the NEW crucial check.
            if norm_path in self.translations:
                translated_target = self.translations[norm_path]
                logging.debug(f"Path {norm_path} is an original path translated to {translated_target}. Reporting ENOENT for original.")
                raise FuseOSError(ENOENT)

            # Priority 2: Is it an explicitly created, purely virtual directory?
            is_purely_virtual = False
            if norm_path in self.virtual_dirs:
                # Check if it *also* corresponds to a physical file/dir (either directly or via translation)
                maps_to_physical = False
                try:
                    # _get_full_path will resolve translations if norm_path is a target
                    # We expect ENOENT if it's purely virtual AND doesn't map to a physical path via translation
                    full_p_check = self._get_full_path(norm_path)
                    if os.path.lexists(full_p_check):
                         maps_to_physical = True
                         logging.debug(f"Virtual path {norm_path} also maps to physical {full_p_check}")
                except FuseOSError as e:
                    # ENOENT here likely means it's purely virtual or the underlying physical path is gone
                    if e.errno != ENOENT:
                        logging.warning(f"Error checking physical mapping for virtual {norm_path}: {e}")
                except Exception as e: # Catch other potential errors
                     logging.warning(f"Unexpected error checking physical mapping for virtual {norm_path}: {e}")

                if not maps_to_physical:
                     is_purely_virtual = True

            if is_purely_virtual:
                 logging.debug(f"Returning virtual directory attrs for purely virtual path {norm_path}")
                 now = time.time()
                 return dict(st_mode=(S_IFDIR | 0o755), st_nlink=2,
                             st_size=0, st_ctime=now, st_mtime=now,
                             st_atime=now, st_uid=os.getuid(), st_gid=os.getgid())

        # Priority 3: Not hidden original, not purely virtual. Get attributes based on translated path.
        try:
            # _get_full_path handles resolving the FUSE path (norm_path) to its underlying physical path,
            # correctly handling cases where norm_path is a translation target.
            full_p = self._get_full_path(norm_path)

            # We need lexists check here *after* _get_full_path which resolves translations.
            if not os.path.lexists(full_p):
                 logging.debug(f"getattr failed for physical path {full_p} (derived from FUSE path {norm_path}): Physical path does not exist.")
                 raise FuseOSError(ENOENT)

            st = os.lstat(full_p)
            logging.debug(f"Got real attrs for FUSE path {norm_path} -> physical path {full_p}")
            return dict((key, getattr(st, key)) for key in ('st_atime', 'st_ctime',
                         'st_gid', 'st_mode', 'st_mtime', 'st_nlink', 'st_size', 'st_uid'))

        except FuseOSError as e:
             # Re-raise specific FUSE errors triggered by _get_full_path or the ENOENT above
             logging.debug(f"getattr for {norm_path} propagating FuseOSError {e.errno}")
             raise
        except OSError as e:
             # Catch OS errors from os.lstat
             logging.error(f"getattr OS error for {norm_path} (physical: {full_p if 'full_p' in locals() else 'unknown'}): {e}")
             raise FuseOSError(e.errno)
        except Exception as e:
             # Catch-all for unexpected errors
             logging.exception(f"Unexpected error during getattr for {norm_path}")
             raise FuseOSError(EACCES) # Default error

    def readdir(self, path, fh):
        logging.debug(f"readdir called for path: {path}")
        final_dirents_set = {'.', '..'}
        special_dirs = {'__all__', '__unplayable__', 'processed'}

        full_p = self._get_full_path(path)
        logging.debug(f"readdir physical path: {full_p}")

        physical_path_exists = os.path.exists(full_p)
        is_physical_dir = os.path.isdir(full_p)

        try:
            if is_physical_dir:
                physical_contents = os.listdir(full_p)
                logging.debug(f"Physical contents for {path}: {physical_contents}")

                with self.fs_lock:
                    original_parent_path = self._translate_path(path) # Get original path of the parent

                    for name in physical_contents:
                        entry_full_path = os.path.join(full_p, name)
                        is_entry_dir = os.path.isdir(entry_full_path) # Still need this check

                        # Construct original path to check against translations
                        original_entry_path = os.path.join(original_parent_path, name)
                        original_entry_path = os.path.normpath(original_entry_path)

                        # Skip if the original path is hidden by a translation
                        if original_entry_path in self.translations:
                            logging.debug(f"Hiding physical entry: {name} (original: {original_entry_path} is translated)")
                            continue

                        # *** The New Check ***
                        # Check if this physical directory is marked as effectively empty
                        if is_entry_dir and entry_full_path in self.physically_empty_parents:
                             logging.debug(f"Hiding directory {name} ({entry_full_path}) as it's marked effectively empty.")
                             continue

                        # Always show special directories if they exist physically and aren't translated away
                        if is_entry_dir and name in special_dirs:
                            final_dirents_set.add(name)
                            logging.debug(f"Including special directory: {name}")
                            continue # Go to next item

                        # Include other directories and files (already passed translation and emptiness checks)
                        final_dirents_set.add(name)
                        logging.debug(f"Including physical entry: {name}")


            elif not physical_path_exists:
                 logging.debug(f"Physical path {full_p} not found, directory might be purely virtual.")
                 with self.fs_lock:
                      if path not in self.dir_structure and path not in self.virtual_dirs:
                           raise FuseOSError(ENOENT)

        except OSError as e:
            if e.errno == ENOENT:
                 with self.fs_lock:
                      if path not in self.dir_structure and path not in self.virtual_dirs:
                           logging.error(f"Error reading physical directory {full_p} and not virtual: {e}")
                           raise FuseOSError(ENOENT)
                      else:
                           logging.debug(f"Ignoring ENOENT for {full_p} as {path} is virtual.")
            else:
                 logging.error(f"Error reading physical directory {full_p}: {e}")
                 raise FuseOSError(e.errno)

        with self.fs_lock:
            if path in self.dir_structure:
                translated_children = self.dir_structure[path]
                logging.debug(f"Adding translated children for {path}: {translated_children}")
                final_dirents_set.update(translated_children)

            for virt_dir in self.virtual_dirs:
                if os.path.dirname(virt_dir) == path and virt_dir != path:
                     child_base_name = os.path.basename(virt_dir)
                     final_dirents_set.add(child_base_name)
                     logging.debug(f"Adding virtual directory child: {child_base_name}")

        logging.debug(f"Final dirents for {path}: {list(final_dirents_set)}")
        return list(final_dirents_set)

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
                remove_virtual_dirs(self.virtual_dirs, self.dir_structure, self.explicit_virtual_dirs, trans_dir)

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
        norm_fuse_path = os.path.normpath(fuse_path)
        with self.fs_lock:
            if norm_fuse_path in self.reverse_translations:
                logging.debug(f"_get_full_path_if_untranslated: Path '{norm_fuse_path}' is directly translated.")
                return None

            parts = norm_fuse_path.strip('/').split('/')
            current_check_path = '/'
            for i, part in enumerate(parts):
                if not part: continue

                if i == 0 and norm_fuse_path.startswith('/'):
                    current_check_path = '/' + part
                elif current_check_path == '/':
                     current_check_path = '/' + part
                else:
                    current_check_path = os.path.join(current_check_path, part)

                logging.debug(f"_get_full_path_if_untranslated: Checking parent '{current_check_path}' for translation.")
                if current_check_path in self.reverse_translations:
                    logging.debug(f"_get_full_path_if_untranslated: Path '{norm_fuse_path}' is under translated parent '{current_check_path}'.")
                    return None

        try:
            path_relative_to_root = norm_fuse_path.lstrip('/')
            full = os.path.join(self.root, path_relative_to_root)
            logging.debug(f"_get_full_path_if_untranslated: Path '{norm_fuse_path}' is untranslated, maps to '{full}'.")
            return full
        except Exception as e:
             logging.error(f"_get_full_path_if_untranslated: Error calculating physical path for '{norm_fuse_path}': {e}")
             return None

    def rename(self, old, new):
        # Log raw arguments received from FUSE layer
        logging.info(f"rename raw args: old='{old}', new='{new}'")
        logging.info(f"rename called: {old} -> {new}")
        norm_old_fuse_path = os.path.normpath(old)
        norm_target_fuse_path = os.path.normpath(new)

        is_source_purely_virtual = False
        original_old_path = None
        full_original_old = None
        try:
            original_old_path = self._translate_path(norm_old_fuse_path)
            full_original_old = os.path.join(self.root, original_old_path.lstrip('/'))
            with self.fs_lock:
                if norm_old_fuse_path in self.virtual_dirs and not os.path.lexists(full_original_old):
                    is_source_purely_virtual = True
            logging.debug(f"Source '{old}' determined as purely virtual: {is_source_purely_virtual}")
        except Exception as e:
            logging.error(f"Error determining source type for rename '{old}': {e}")
            raise FuseOSError(EACCES)

        if is_source_purely_virtual:
            logging.info(f"Attempting rename of purely virtual directory: {old} -> {new}")

            with self.fs_lock:
                if norm_target_fuse_path in self.virtual_dirs or norm_target_fuse_path in self.reverse_translations:
                    logging.error(f"Rename failed: Target '{new}' already exists as a virtual directory or translation.")
                    raise FuseOSError(EEXIST)
                potential_physical_target = self._get_full_path_if_untranslated(norm_target_fuse_path)
                if potential_physical_target and os.path.lexists(potential_physical_target):
                    logging.error(f"Rename failed: Target '{new}' conflicts with an existing physical path '{potential_physical_target}'.")
                    raise FuseOSError(EEXIST)

            source_was_explicit = False
            with self.fs_lock:
                 if norm_old_fuse_path in self.explicit_virtual_dirs:
                      source_was_explicit = True

            child_translation_updates = []
            updated_virtual_dirs = set()
            affected_originals = set()

            with self.fs_lock:
                prefix_to_match = norm_old_fuse_path + ('/' if norm_old_fuse_path != '/' else '')
                len_prefix = len(prefix_to_match)

                for orig, trans in list(self.translations.items()):
                    if trans.startswith(prefix_to_match):
                        suffix = trans[len_prefix:]
                        new_trans = os.path.join(norm_target_fuse_path, suffix)
                        child_translation_updates.append((orig, new_trans))
                        affected_originals.add(orig)
                        logging.debug(f"  - Planning update for child translation: {orig} -> {new_trans}")

                for v_dir in list(self.virtual_dirs):
                    if v_dir == norm_old_fuse_path:
                        continue
                    if v_dir.startswith(prefix_to_match):
                        suffix = v_dir[len_prefix:]
                        new_v_dir = os.path.join(norm_target_fuse_path, suffix)
                        updated_virtual_dirs.add(new_v_dir)
                        logging.debug(f"  - Planning update for child virtual dir: {v_dir} -> {new_v_dir}")
                        self.virtual_dirs.discard(v_dir)

            virtual_rename_info = (norm_old_fuse_path, norm_target_fuse_path) if source_was_explicit else None
            db_op = self._bulk_update_translations
            db_args = (child_translation_updates, virtual_rename_info)
            result_queue = Queue()
            logging.info(f"Queueing bulk update for {len(child_translation_updates)} child translations. Virtual rename explicit: {source_was_explicit}")
            self.db_queue.put((db_op, db_args, result_queue))

            try:
                success_or_error = result_queue.get(timeout=20)

                if isinstance(success_or_error, Exception):
                     logging.error(f"Rename failed: DB worker returned an exception during bulk update: {success_or_error}")
                     raise FuseOSError(getattr(success_or_error, 'errno', EACCES))
                elif not success_or_error:
                     logging.error(f"Rename failed: DB bulk update operation returned False for {old} -> {new}")
                     raise FuseOSError(EACCES)
                else:
                     logging.info(f"DB bulk update successful for renaming {old} -> {new}")
                     with self.fs_lock:
                          if source_was_explicit:
                              self.explicit_virtual_dirs.discard(norm_old_fuse_path)
                              self.explicit_virtual_dirs.add(norm_target_fuse_path)
                          self.virtual_dirs.discard(norm_old_fuse_path)
                          self.virtual_dirs.add(norm_target_fuse_path)
                          self.virtual_dirs.update(updated_virtual_dirs)
                          add_virtual_dirs(self.virtual_dirs, norm_target_fuse_path)

                          old_parent = os.path.dirname(norm_old_fuse_path) or '/'
                          new_parent = os.path.dirname(norm_target_fuse_path) or '/'
                          old_basename = os.path.basename(norm_old_fuse_path)
                          new_basename = os.path.basename(norm_target_fuse_path)

                          if old_parent in self.dir_structure:
                              self.dir_structure[old_parent].discard(old_basename)
                              if not self.dir_structure[old_parent]:
                                  del self.dir_structure[old_parent]

                          if new_parent not in self.dir_structure:
                              self.dir_structure[new_parent] = set()
                          self.dir_structure[new_parent].add(new_basename)

                          if norm_old_fuse_path in self.dir_structure:
                              self.dir_structure[norm_target_fuse_path] = self.dir_structure.pop(norm_old_fuse_path)
                          elif child_translation_updates:
                              if norm_target_fuse_path not in self.dir_structure:
                                   self.dir_structure[norm_target_fuse_path] = set()

                          for orig, new_trans in child_translation_updates:
                              old_trans = self.translations.pop(orig, None)
                              if old_trans:
                                  self.reverse_translations.pop(old_trans, None)

                              self.translations[orig] = new_trans
                              self.reverse_translations[new_trans] = orig

                              new_trans_parent = os.path.dirname(new_trans) or '/'
                              new_trans_basename = os.path.basename(new_trans)
                              if new_trans_parent not in self.dir_structure:
                                   self.dir_structure[new_trans_parent] = set()
                              self.dir_structure[new_trans_parent].add(new_trans_basename)

                              if old_trans:
                                  old_trans_parent = os.path.dirname(old_trans) or '/'
                                  if old_trans_parent != new_trans_parent and old_trans_parent in self.dir_structure:
                                       old_trans_basename = os.path.basename(old_trans)
                                       self.dir_structure[old_trans_parent].discard(old_trans_basename)
                                       if not self.dir_structure[old_trans_parent]:
                                            del self.dir_structure[old_trans_parent]

                          self._get_full_path.cache_clear()
                          self.file_handle_cache.invalidate(norm_old_fuse_path)
                          self.file_handle_cache.invalidate(norm_target_fuse_path)

                          self.update_event.set()
                          self._check_and_update_parent_emptiness(old)
                          return 0

            except Empty:
                logging.error("Rename failed: DB worker timed out during bulk update.")
                raise FuseOSError(EACCES)
            except FuseOSError:
                raise
            except Exception as e:
                logging.exception(f"Unexpected error handling virtual rename result for {old} -> {new}")
                raise FuseOSError(EACCES)

        else:
            logging.info(f"Attempting rename involving physical path or existing translation: {old} -> {new}")
            if not os.path.lexists(full_original_old):
                logging.error(f"Rename failed: Source '{old}' (original: '{original_old_path}') does not exist physically.")
                raise FuseOSError(ENOENT)

            norm_original_old_path = os.path.normpath(original_old_path)
            logging.debug(f"Rename normalized: original='{norm_original_old_path}', target='{norm_target_fuse_path}'")

            db_op = None
            db_args = None

            if norm_target_fuse_path == norm_original_old_path or norm_target_fuse_path.startswith(norm_original_old_path.rstrip('/') + '/'):
                 if norm_target_fuse_path == norm_original_old_path:
                     logging.info(f"Detected rename back to original path: {old} ({norm_original_old_path}) -> {new}. Removing translation if it exists.")
                     with self.fs_lock:
                         if norm_old_fuse_path in self.reverse_translations:
                             db_op = self._remove_translation
                             db_args = (norm_original_old_path,)
                         else:
                             logging.info(f"Rename source '{old}' was not translated; rename to original path '{new}' is a no-op.")
                             db_op = "NO_OP"
                 else:
                     logging.error(f"Rename failed: Cannot rename '{old}' (original: '{norm_original_old_path}') into itself ('{new}').")
                     raise FuseOSError(EINVAL)
            else:
                if norm_old_fuse_path.startswith(norm_target_fuse_path.rstrip('/') + '/'):
                     logging.error(f"Rename failed: Cannot rename '{old}' to an ancestor directory '{new}'.")
                     raise FuseOSError(EINVAL)

                potential_physical_target = self._get_full_path_if_untranslated(norm_target_fuse_path)
                target_physically_exists = potential_physical_target and os.path.lexists(potential_physical_target)

                if target_physically_exists:
                     with self.fs_lock:
                          is_target_fuse_path_translated = norm_target_fuse_path in self.reverse_translations
                     if not is_target_fuse_path_translated:
                          logging.error(f"Rename failed: Target '{new}' conflicts with an existing physical path '{potential_physical_target}' that is not managed by a translation.")
                          raise FuseOSError(EEXIST)
                     else:
                          logging.debug(f"Target '{new}' conflicts with physical path '{potential_physical_target}', but target FUSE path is already translated. Allowing overwrite.")

                logging.info(f"Adding/updating translation for rename: {old} ({norm_original_old_path}) -> {new} ({norm_target_fuse_path})")
                db_op = self._add_translation
                db_args = (norm_original_old_path, norm_target_fuse_path)

            if db_op == "NO_OP":
                 logging.debug("Rename to original path completed as no-op (source wasn't translated).")
                 return 0

            if db_op is None or db_args is None:
                 logging.error("Rename failed: Internal logic error, no DB operation determined.")
                 raise FuseOSError(EACCES)

            result_queue = Queue()
            self.db_queue.put((db_op, db_args, result_queue))

            try:
                success_or_error = result_queue.get(timeout=10)

                if isinstance(success_or_error, Exception):
                     logging.error(f"Rename failed: DB worker returned an exception: {success_or_error}")
                     raise FuseOSError(getattr(success_or_error, 'errno', EACCES))
                elif not success_or_error:
                     if db_op == self._add_translation:
                         logging.error(f"Rename failed: DB add/update operation returned False for {norm_original_old_path} -> {norm_target_fuse_path}")
                         raise FuseOSError(EACCES)
                     else:
                          logging.info(f"Rename to original: DB remove operation returned False (likely no existing translation found), proceeding.")
                          self._get_full_path.cache_clear()
                          self.file_handle_cache.invalidate(norm_old_fuse_path)
                          self.file_handle_cache.invalidate(norm_target_fuse_path)

                else:
                    logging.info(f"Rename DB operation successful for {old} -> {new}")
                    self._get_full_path.cache_clear()
                    self.file_handle_cache.invalidate(norm_old_fuse_path)
                    self.file_handle_cache.invalidate(norm_target_fuse_path)

                self.update_event.set()
                self._check_and_update_parent_emptiness(old)
                return 0

            except Empty:
                logging.error("Rename failed: DB worker timed out.")
                raise FuseOSError(EACCES)
            except FuseOSError:
                raise
            except Exception as e:
                logging.exception(f"Unexpected error handling rename result for {old} -> {new}")
                raise FuseOSError(EACCES)

    def _bulk_update_translations(self, updates, virtual_rename_info=None):
        action = "bulk translation update"
        if virtual_rename_info:
            action += f" and rename virtual dir {virtual_rename_info[0]} -> {virtual_rename_info[1]}"
        logging.debug(f"DB Worker: Starting {action} for {len(updates)} translations.")
        try:
            with self.db_lock:
                cursor = self.conn.cursor()
                cursor.execute("BEGIN TRANSACTION;")
                try:
                    for original, new_translated in updates:
                        cursor.execute('INSERT OR REPLACE INTO translations (original, translated) VALUES (?, ?)',
                                       (original, new_translated))

                    if virtual_rename_info:
                        old_vpath, new_vpath = virtual_rename_info
                        logging.debug(f"DB Worker: Updating explicit virtual dir entry {old_vpath} -> {new_vpath}")
                        cursor.execute('DELETE FROM explicit_virtual_dirs WHERE path = ?', (old_vpath,))
                        cursor.execute('INSERT OR IGNORE INTO explicit_virtual_dirs (path) VALUES (?)', (new_vpath,))

                    cursor.execute("COMMIT;")
                    logging.info(f"DB Worker: {action} successful.")
                    return True
                except sqlite3.Error as e:
                    logging.error(f"DB Worker: Error during {action} transaction, rolling back: {e}")
                    cursor.execute("ROLLBACK;")
                    return False
                finally:
                    cursor.close()
        except sqlite3.Error as e:
            logging.error(f"DB Worker: Error obtaining cursor or managing transaction for {action}: {e}")
            return False
        except Exception as e:
            logging.exception("DB Worker: Unexpected error during transaction")
            try:
                with self.db_lock:
                    cursor = self.conn.cursor()
                    cursor.execute("ROLLBACK;")
                    cursor.close()
            except Exception as rb_e:
                logging.error(f"DB Worker: Failed to rollback after unexpected error: {rb_e}")
            return False

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
        logging.debug(f"mkdir attempt for path: {path} with mode {mode:#o}")
        norm_path = os.path.normpath(path)
        parent_dir = os.path.dirname(norm_path) or '/'
        child_name = os.path.basename(norm_path)

        if not child_name:
            logging.error(f"mkdir failed: Invalid path '{path}' (empty basename)")
            raise FuseOSError(EINVAL)

        with self.fs_lock:
            parent_exists = False
            if parent_dir in self.virtual_dirs:
                parent_exists = True
                logging.debug(f"Parent '{parent_dir}' exists virtually.")
            else:
                try:
                    full_parent_path = self._get_full_path(parent_dir)
                    if os.path.isdir(full_parent_path):
                        parent_exists = True
                        logging.debug(f"Parent '{parent_dir}' exists physically at '{full_parent_path}'.")
                    else:
                         logging.warning(f"mkdir failed: Parent '{parent_dir}' (physical: '{full_parent_path}') is not a directory.")
                         raise FuseOSError(ENOTDIR)
                except FuseOSError as e:
                    if e.errno == ENOENT:
                        logging.warning(f"mkdir failed: Parent directory '{parent_dir}' does not exist.")
                        raise FuseOSError(ENOENT)
                    else:
                        logging.error(f"Error checking parent '{parent_dir}' existence: {e}")
                        raise
                except OSError as e:
                     logging.error(f"OS error checking parent '{parent_dir}' existence: {e}")
                     raise FuseOSError(e.errno)

            if not parent_exists:
                logging.error(f"mkdir failed: Parent directory '{parent_dir}' not found.")
                raise FuseOSError(ENOENT)

            target_exists = False
            if norm_path in self.virtual_dirs:
                target_exists = True
                logging.warning(f"mkdir failed: Path '{path}' already exists as a virtual directory.")
            elif norm_path in self.reverse_translations:
                target_exists = True
                logging.warning(f"mkdir failed: Path '{path}' already exists as a translation target.")
            else:
                try:
                    full_target_path = self._get_full_path(norm_path)
                    if os.path.lexists(full_target_path):
                        target_exists = True
                        logging.warning(f"mkdir failed: Path '{path}' conflicts with existing physical path '{full_target_path}'.")
                except FuseOSError as e:
                     if e.errno != ENOENT:
                          logging.error(f"Error checking target '{norm_path}' existence: {e}")
                          raise
                except OSError as e:
                     logging.error(f"OS error checking target '{norm_path}' existence: {e}")
                     raise FuseOSError(e.errno)

            if target_exists:
                raise FuseOSError(EEXIST)

            logging.info(f"Creating virtual directory: {norm_path}")
            add_virtual_dirs(self.virtual_dirs, norm_path)
            self.virtual_dirs.add(norm_path)
            self.explicit_virtual_dirs.add(norm_path)
            if parent_dir not in self.dir_structure:
                 self.dir_structure[parent_dir] = set()
            self.dir_structure[parent_dir].add(child_name)

        self.db_queue.put((self._add_explicit_virtual_dir, (norm_path,), None))

        return 0

    def rmdir(self, path):
        logging.debug(f"rmdir attempt for path: {path}")
        norm_path = os.path.normpath(path)

        if norm_path == '/':
            logging.error("rmdir failed: Cannot remove root directory.")
            raise FuseOSError(EACCES) # Or EINVAL

        with self.fs_lock:
            is_explicit_virtual = norm_path in self.explicit_virtual_dirs
            is_known_virtual = norm_path in self.virtual_dirs # General check (covers explicit and implicit)
            is_translation_target = norm_path in self.reverse_translations

            full_p_check = None
            physical_exists = False
            is_physical_dir = False

            try:
                # Check physical/translated state regardless of virtual status initially
                # This resolves translations if norm_path is a target
                full_p_check = self._get_full_path(norm_path)
                physical_exists = os.path.lexists(full_p_check)
                if physical_exists:
                    is_physical_dir = os.path.isdir(full_p_check)
            except FuseOSError as e:
                if e.errno != ENOENT:
                    logging.error(f"Error checking path status for rmdir '{path}': {e}")
                    raise # Re-raise unexpected FUSE errors
                # ENOENT is expected if it's purely virtual or truly doesn't exist
            except OSError as e:
                logging.error(f"OS error checking path status for rmdir '{path}': {e}")
                raise FuseOSError(e.errno)
            except Exception as e:
                 logging.exception(f"Unexpected error checking path status for rmdir '{path}'")
                 raise FuseOSError(EACCES)

            # --- Decision Logic ---

            # Condition 1: Trying to remove a direct translation target? Deny.
            # User should remove the translation itself (e.g., via setxattr).
            if is_translation_target:
                logging.warning(f"rmdir failed: Path '{path}' is a direct translation target. Remove the translation instead.")
                # EACCES is suitable as the operation is disallowed on this type of object through rmdir.
                raise FuseOSError(EACCES)

            # Condition 2: Trying to remove a path corresponding to a real, non-translated directory? Deny.
            # Check if the path *isn't* translated AND corresponds to a physical dir.
            # We need _translate_path here specifically to see if norm_path itself resolves to something else.
            original_path_check = self._translate_path(norm_path)
            if original_path_check == norm_path and is_physical_dir:
                 logging.warning(f"rmdir failed: Path '{path}' corresponds to an underlying physical directory ('{full_p_check}'). Deletion via rmdir is not permitted.")
                 raise FuseOSError(EACCES) # Operation not permitted on underlying physical items

            # Condition 3: Path exists physically but is not a directory? Error.
            if physical_exists and not is_physical_dir:
                 logging.warning(f"rmdir failed: Path '{path}' exists physically but is not a directory ('{full_p_check}').")
                 raise FuseOSError(ENOTDIR)

            # Condition 4: Is it a known virtual directory (explicit or implicit)? Proceed to check emptiness.
            if is_known_virtual:
                # Check emptiness (both translated children and nested virtual dirs)
                is_empty = True
                # Check for translated children within this directory in the virtual structure
                if norm_path in self.dir_structure and self.dir_structure[norm_path]:
                    logging.warning(f"rmdir failed: Directory '{path}' is not empty (contains translated items: {self.dir_structure[norm_path]}).")
                    is_empty = False

                # Check for nested virtual directory children (more robust check)
                if is_empty: # Only check if not already found to be non-empty
                    # Ensure comparison is against paths starting with the directory + separator
                    prefix_to_check = norm_path.rstrip('/') + '/'
                    for v_dir in self.virtual_dirs:
                        # Ensure it's a *child*, not the directory itself, and check prefix
                        if v_dir != norm_path and v_dir.startswith(prefix_to_check):
                             # Check if the child is directly under norm_path
                             relative_path = os.path.relpath(v_dir, norm_path)
                             if '/' not in relative_path and relative_path != '.':
                                 logging.warning(f"rmdir failed: Directory '{path}' is not empty (contains virtual directory '{os.path.basename(v_dir)}').")
                                 is_empty = False
                                 break # Found one, no need to check more

                if not is_empty:
                    raise FuseOSError(errno.ENOTEMPTY) # Directory not empty

                # --- Perform Deletion from memory ---
                logging.info(f"Removing {'explicitly' if is_explicit_virtual else 'implicitly'} virtual directory: {norm_path}")

                if is_explicit_virtual:
                    self.explicit_virtual_dirs.discard(norm_path)
                self.virtual_dirs.discard(norm_path) # Remove from the general virtual set

                parent_dir = os.path.dirname(norm_path) or '/'
                child_name = os.path.basename(norm_path)
                if parent_dir in self.dir_structure:
                    self.dir_structure[parent_dir].discard(child_name)
                    if not self.dir_structure[parent_dir]:
                        # Only remove parent from dir_structure if it becomes empty
                        del self.dir_structure[parent_dir]
                        # NOTE: We are not automatically removing the parent from virtual_dirs here.
                        # It might still be needed if it was explicitly created or is implicitly
                        # needed for other unrelated translations. remove_virtual_dirs could be
                        # called but might be too aggressive or complex here.

                # If the directory itself had an entry (e.g., due to past children), remove it.
                if norm_path in self.dir_structure:
                    del self.dir_structure[norm_path]

                # --- Queue DB operation *only* if it was explicit ---
                # Implicit virtual directories don't have a DB entry to remove.
                if is_explicit_virtual:
                    # Queue DB deletion *after* releasing the fs_lock
                    pass # DB operation moved outside the lock
            else:
                # Condition 5: Not virtual, not physical/translated, doesn't exist.
                # This case covers paths that don't map to anything known.
                logging.warning(f"rmdir failed: Path '{path}' does not correspond to a known directory (virtual, physical, or translated).")
                raise FuseOSError(ENOENT)

        # ---- Outside fs_lock ----
        # Queue DB operation if needed (must be done after lock release)
        if is_known_virtual and is_explicit_virtual and is_empty: # Ensure it was removable before queuing DB op
            logging.debug(f"Queueing DB removal for explicitly created virtual directory: {norm_path}")
            self.db_queue.put((self._remove_explicit_virtual_dir, (norm_path,), None))

        # Trigger potential watchers if removal happened
        if is_known_virtual and is_empty:
            self.update_event.set()
            return 0 # Success

        # If we reached here something went wrong or a condition wasn't met properly
        # This path should ideally not be reached if logic above is sound.
        logging.error(f"rmdir for '{path}' reached unexpected end state.")
        raise FuseOSError(EACCES) # Fallback error

    def chmod(self, path, mode):
        logging.warning(f"Denied chmod attempt for {path}")
        raise FuseOSError(EROFS)

    def chown(self, path, uid, gid):
        logging.warning(f"Denied chown attempt for {path}")
        raise FuseOSError(EROFS)

    def _add_explicit_virtual_dir(self, path):
        logging.debug(f"DB Worker: Adding explicit virtual dir: {path}")
        try:
            with self.db_lock:
                cursor = self.conn.cursor()
                cursor.execute('INSERT OR IGNORE INTO explicit_virtual_dirs (path) VALUES (?)', (path,))
                self.conn.commit()
                cursor.close()
            return True
        except sqlite3.Error as e:
            logging.error(f"DB Error adding explicit virtual dir {path}: {e}")
            return False

    def _remove_explicit_virtual_dir(self, path):
        logging.debug(f"DB Worker: Removing explicit virtual dir: {path}")
        try:
            with self.db_lock:
                cursor = self.conn.cursor()
                cursor.execute('DELETE FROM explicit_virtual_dirs WHERE path = ?', (path,))
                self.conn.commit()
                cursor.close()
            return True
        except sqlite3.Error as e:
            logging.error(f"DB Error removing explicit virtual dir {path}: {e}")
            return False

    def setxattr(self, path, name, value, options, position=0):
        """
        Handles setting extended attributes. Intercepts a specific attribute
        to control translations (e.g., remove a translation).
        Otherwise, denies the operation as the FS is read-only for xattrs.
        """
        logging.debug(f"setxattr called for path: {path}, name: {name}, value: {value[:20]}...") # Log truncated value

        CONTROL_ATTR_NAME = b'user.translation.control' # Use bytes
        REMOVE_VALUE = b'remove'

        # Decode name/value if they are strings (less likely from OS but good practice)
        attr_name_bytes = name.encode('utf-8') if isinstance(name, str) else name
        value_bytes = value.encode('utf-8') if isinstance(value, str) else value

        if attr_name_bytes == CONTROL_ATTR_NAME:
            if value_bytes == REMOVE_VALUE:
                logging.info(f"Received translation removal request via setxattr for path: {path}")
                norm_path = os.path.normpath(path)

                with self.fs_lock:
                    if norm_path in self.reverse_translations:
                        original_path = self.reverse_translations[norm_path]
                        logging.info(f"Queueing removal of translation for original path: {original_path} (triggered by {path})")

                        # Queue the actual removal operation to the DB worker thread
                        # Don't wait for a result here, just queue it.
                        self.db_queue.put((self._remove_translation, (original_path,), None)) # No result_queue needed

                        # Assume success at this point (task is queued)
                        # Trigger watches/updates immediately
                        self.update_event.set()
                        logging.info(f"setxattr remove for {original_path} queued successfully.")
                        return 0 # Return success to FUSE immediately

                    else:
                        logging.warning(f"setxattr remove failed: Path '{path}' not found in reverse translations.")
                        raise FuseOSError(ENOENT) # The path doesn't represent a known translation target
            else:
                logging.warning(f"setxattr denied: Unsupported value '{value_bytes[:20]}...' for control attribute '{CONTROL_ATTR_NAME.decode()}' on {path}")
                raise FuseOSError(EINVAL) # Invalid argument/value for the control attribute
        else:
            # Deny setting any other extended attributes
            logging.warning(f"setxattr denied: Attempt to set unsupported attribute '{attr_name_bytes.decode()}' on {path}")
            raise FuseOSError(EROFS) # Filesystem is read-only for other attributes

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

    def _check_and_update_parent_emptiness(self, original_path):
        """Checks if the physical parent of original_path is now empty
           (only contains translated items) and updates the set."""
        original_parent = os.path.dirname(original_path)
        if not original_parent or original_parent == '/':
             return # Cannot hide root or direct children of root this way

        # Map the FUSE parent path to its physical counterpart
        # This might be tricky if the parent itself is virtual/translated
        # Let's assume _get_full_path works correctly for the parent.
        try:
             physical_parent_path = self._get_full_path(original_parent)
        except FuseOSError:
             logging.warning(f"Could not get physical path for parent {original_parent} during emptiness check.")
             return # Can't check emptiness

        if not os.path.isdir(physical_parent_path):
            # Parent doesn't exist physically or isn't a dir, shouldn't happen if child existed.
            return

        try:
            physical_contents = os.listdir(physical_parent_path)
            is_effectively_empty = True
            for name in physical_contents:
                # Construct the original path of the sibling
                potential_original_sibling = os.path.join(original_parent, name)
                potential_original_sibling = os.path.normpath(potential_original_sibling)
                # If any sibling is NOT translated, the parent is not effectively empty
                if potential_original_sibling not in self.translations:
                    is_effectively_empty = False
                    break

            if is_effectively_empty:
                logging.info(f"Physical directory {physical_parent_path} (parent of {original_path}) is now effectively empty. Marking for hiding.")
                self.physically_empty_parents.add(physical_parent_path)
            else:
                # Ensure it's not in the set if it's not empty
                if physical_parent_path in self.physically_empty_parents:
                    logging.info(f"Physical directory {physical_parent_path} (parent of {original_path}) is no longer effectively empty. Unmarking.")
                    self.physically_empty_parents.discard(physical_parent_path)

        except OSError as e:
            logging.error(f"Error checking emptiness of {physical_parent_path}: {e}")
            # Safer to assume not empty on error
            self.physically_empty_parents.discard(physical_parent_path)

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
TranslationFS.setxattr = fuse_error_handler(TranslationFS.setxattr)
