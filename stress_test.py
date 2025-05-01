import os
import random
import string
import threading
import time
import logging
import errno
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

# --- Configuration ---
MOUNT_POINT = "/mnt/zurg-docker-testing"  # User-specified mount point
NUM_THREADS = 10
OPERATIONS_PER_THREAD = 150 # Increased slightly to compensate for removing file reads
TEST_BASE_DIR_NAME = "test" # User-specified base directory *name* within MOUNT_POINT
LOG_FILE = "stress_test.log"

# List of KNOWN PHYSICAL paths (relative to MOUNT_POINT) that exist before the test
# These will be targets for rename (translation creation)
# IMPORTANT: Ensure these actually exist in your physical backend (`root`)
KNOWN_PHYSICAL_ITEMS_REL = [
    "nba",
    "movies/The.Old.Guard.2020.1080p.WEBRip.x265-RARBG",
    # Add more physical files/dirs here if needed
]
# Convert to full paths within the mount point
KNOWN_PHYSICAL_ITEMS_FULL = [os.path.join(MOUNT_POINT, p) for p in KNOWN_PHYSICAL_ITEMS_REL]

# --- Setup Logging ---
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s',
                    handlers=[logging.FileHandler(LOG_FILE, mode='w'), # Overwrite log each run
                              logging.StreamHandler()])

# --- Thread-Local Storage for Created Virtual Dirs ---
# Using thread-local storage avoids lock contention for this state
thread_local = threading.local()

def get_thread_virtual_dirs():
    """Gets the set of virtual dirs created by the current thread."""
    if not hasattr(thread_local, 'virtual_dirs'):
        thread_local.virtual_dirs = set()
    return thread_local.virtual_dirs

# --- Helper Functions ---
def random_string(length=8):
    """Generates a random string."""
    return ''.join(random.choice(string.ascii_lowercase) for _ in range(length))

def generate_random_virtual_path(thread_base_dir, depth=3):
    """Generates a random virtual path within the thread's test base directory."""
    parts = [thread_base_dir] + [random_string() for _ in range(random.randint(1, depth))]
    # No join with MOUNT_POINT here, path is relative to MOUNT_POINT for internal logic
    # The operations functions will join with MOUNT_POINT
    return os.path.join(*parts)

# --- Focused Filesystem Operations ---

def create_virtual_dir_op(thread_base_dir):
    """Attempts to create a new virtual directory."""
    vpath_rel = generate_random_virtual_path(thread_base_dir)
    vpath_full = os.path.join(MOUNT_POINT, vpath_rel)
    created_dirs_set = get_thread_virtual_dirs()
    try:
        # Use makedirs for resilience against missing intermediate paths created by *other* threads
        os.makedirs(vpath_full, exist_ok=True)
        # Check if it *actually* created it or if it existed from another thread/run
        # Note: This check itself isn't perfectly atomic, but helps track what *this* thread likely created
        if vpath_rel not in created_dirs_set:
             created_dirs_set.add(vpath_rel)
             logging.info(f"Created dir: {vpath_full}")
             return True # Count as success if makedirs passes
        else:
             logging.debug(f"Dir likely existed (created by other thread?): {vpath_full}")
             return True # Still a success from OS perspective if exist_ok=True

    except OSError as e:
        # EEXIST might still happen with exist_ok=False, or other errors like ENOTDIR if path conflict
        if e.errno == errno.EEXIST:
             logging.warning(f"Failed creating dir (EEXIST - race?): {vpath_full}: {e}")
        elif e.errno == errno.ENOENT:
             logging.warning(f"Failed creating dir (ENOENT - parent race?): {vpath_full}: {e}")
        else:
            logging.error(f"Error creating dir {vpath_full}: {e}")
        return False

def remove_virtual_dir_op(thread_base_dir):
    """Attempts to remove a virtual directory previously created by this thread."""
    created_dirs_set = get_thread_virtual_dirs()
    if not created_dirs_set:
        logging.debug("No virtual dirs tracked by this thread to remove.")
        return False # Can't succeed if nothing to remove

    # Choose a random dir this thread created
    vpath_rel_to_remove = random.choice(list(created_dirs_set))
    vpath_full_to_remove = os.path.join(MOUNT_POINT, vpath_rel_to_remove)

    try:
        os.rmdir(vpath_full_to_remove)
        logging.info(f"Removed dir: {vpath_full_to_remove}")
        created_dirs_set.remove(vpath_rel_to_remove) # Remove from tracked set on SUCCESS
        return True
    except OSError as e:
        # These errors are expected in concurrent tests
        if e.errno == errno.ENOTEMPTY:
            logging.warning(f"Failed removing dir (ENOTEMPTY - expected race): {vpath_full_to_remove}: {e}")
        elif e.errno == errno.ENOENT:
            logging.warning(f"Failed removing dir (ENOENT - already removed?): {vpath_full_to_remove}: {e}")
        elif e.errno == errno.EACCES:
             logging.warning(f"Failed removing dir (EACCES - likely not virtual or permission): {vpath_full_to_remove}: {e}")
        elif e.errno == errno.ENOTDIR:
             logging.warning(f"Failed removing dir (ENOTDIR - conflict?): {vpath_full_to_remove}: {e}")
        else:
            logging.error(f"Error removing dir {vpath_full_to_remove}: {e}")

        # If removal failed (e.g. ENOTEMPTY), maybe remove from set anyway if it's unlikely to succeed later?
        # For now, keep it in the set - maybe something else will empty it.
        return False


def rename_virtual_dir_op(thread_base_dir):
    """Attempts to rename a virtual directory created by this thread to a new virtual path."""
    created_dirs_set = get_thread_virtual_dirs()
    if not created_dirs_set:
        logging.debug("No virtual dirs tracked by this thread to rename.")
        return False

    old_vpath_rel = random.choice(list(created_dirs_set))
    old_vpath_full = os.path.join(MOUNT_POINT, old_vpath_rel)

    new_vpath_rel = generate_random_virtual_path(thread_base_dir)
    new_vpath_full = os.path.join(MOUNT_POINT, new_vpath_rel)

    # Avoid renaming to self or existing tracked path (basic check)
    if old_vpath_rel == new_vpath_rel or new_vpath_rel in created_dirs_set:
        logging.debug(f"Skipping rename to self or existing tracked path: {new_vpath_rel}")
        return False

    try:
        # Ensure parent of new_path exists (might be needed if depth > 1)
        # This can race with rmdir ops!
        os.makedirs(os.path.dirname(new_vpath_full), exist_ok=True)

        os.rename(old_vpath_full, new_vpath_full)
        logging.info(f"Renamed Virtual: {old_vpath_full} -> {new_vpath_full}")
        # Update tracked set on SUCCESS
        created_dirs_set.remove(old_vpath_rel)
        created_dirs_set.add(new_vpath_rel)
        return True
    except OSError as e:
        # Expected errors: ENOENT (source gone), EEXIST (target exists), ENOTEMPTY (maybe on target?), EACCES, EINVAL, ENOTDIR
        log_level = logging.WARNING if e.errno in (errno.ENOENT, errno.EEXIST, errno.ENOTEMPTY, errno.EACCES, errno.EINVAL, errno.ENOTDIR) else logging.ERROR
        logging.log(log_level, f"Failed renaming virtual {old_vpath_full} -> {new_vpath_full}: {e}")

        # If source doesn't exist, remove from our tracked set
        if e.errno == errno.ENOENT and old_vpath_rel in created_dirs_set:
            logging.debug(f"Removing {old_vpath_rel} from tracked set as it seems gone.")
            created_dirs_set.discard(old_vpath_rel)
        return False

def rename_physical_to_virtual_op(thread_base_dir):
    """Attempts to rename a known physical item to a new virtual path (create translation)."""
    if not KNOWN_PHYSICAL_ITEMS_FULL:
        logging.debug("No known physical items defined to test rename.")
        return False

    # Choose a random physical item to try and translate
    # Note: Once translated, subsequent attempts on the *original* name WILL fail (ENOENT) - this is expected!
    physical_path_full = random.choice(KNOWN_PHYSICAL_ITEMS_FULL)

    # Generate a target virtual path
    target_vpath_rel = generate_random_virtual_path(thread_base_dir)
    target_vpath_full = os.path.join(MOUNT_POINT, target_vpath_rel)

    try:
        # Ensure parent of new_path exists (can race!)
        os.makedirs(os.path.dirname(target_vpath_full), exist_ok=True)

        os.rename(physical_path_full, target_vpath_full)
        logging.info(f"Renamed Physical->Virtual: {physical_path_full} -> {target_vpath_full}")
        # We don't track these translations specifically in the test script,
        # but the FS should now handle `target_vpath_full`.
        # IMPORTANT: We don't remove `physical_path_full` from KNOWN_PHYSICAL_ITEMS_FULL
        # because another thread might successfully rename it *back* or the FS
        # might allow renaming the *new* virtual path back to the original name.
        # Accepting ENOENT on future attempts for this source is part of the test.
        return True
    except OSError as e:
        log_level = logging.WARNING if e.errno in (errno.ENOENT, errno.EEXIST, errno.EACCES, errno.EINVAL, errno.ENOTDIR) else logging.ERROR
        logging.log(log_level, f"Failed renaming physical->virtual {physical_path_full} -> {target_vpath_full}: {e}")
        return False


def list_dir_op(thread_base_dir):
    """Lists entries in various directories."""
    created_dirs_set = get_thread_virtual_dirs()
    potential_targets = [MOUNT_POINT, os.path.join(MOUNT_POINT, thread_base_dir)]
    if created_dirs_set:
        potential_targets.extend([os.path.join(MOUNT_POINT, p) for p in created_dirs_set])
        potential_targets.extend([os.path.join(MOUNT_POINT, os.path.dirname(p)) for p in created_dirs_set]) # Add parents
    # Add known physical dirs if any are directories (simple check)
    potential_targets.extend([p for p in KNOWN_PHYSICAL_ITEMS_FULL if '.' not in os.path.basename(p)]) # Heuristic for dirs

    target_dir = random.choice(potential_targets)

    try:
        entries = os.listdir(target_dir)
        logging.info(f"Listed dir {target_dir} ({len(entries)} entries)")
        return True
    except OSError as e:
        log_level = logging.WARNING if e.errno in (errno.ENOENT, errno.ENOTDIR, errno.EACCES) else logging.ERROR
        logging.log(log_level, f"Failed listing dir {target_dir}: {e}")
        return False

def get_attrs_op(thread_base_dir):
    """Gets attributes for various items."""
    created_dirs_set = get_thread_virtual_dirs()
    potential_targets = [MOUNT_POINT, os.path.join(MOUNT_POINT, thread_base_dir)]
    if created_dirs_set:
        potential_targets.extend([os.path.join(MOUNT_POINT, p) for p in created_dirs_set])
    potential_targets.extend(KNOWN_PHYSICAL_ITEMS_FULL) # Check original physical paths too

    target_path = random.choice(potential_targets)

    try:
        os.stat(target_path)
        logging.info(f"Got attrs for: {target_path}")
        return True
    except OSError as e:
        log_level = logging.WARNING if e.errno in (errno.ENOENT, errno.EACCES) else logging.ERROR
        logging.log(log_level, f"Failed getting attrs for {target_path}: {e}")
        # If ENOENT on a tracked virtual dir, maybe remove it?
        target_rel = os.path.relpath(target_path, MOUNT_POINT)
        if e.errno == errno.ENOENT and target_rel in created_dirs_set:
             logging.debug(f"Removing {target_rel} from tracked set as stat got ENOENT.")
             created_dirs_set.discard(target_rel)
        return False

# --- Worker Function ---
def worker(worker_id):
    # Ensure thread-local storage is initialized for this thread
    get_thread_virtual_dirs()
    thread_base_rel = os.path.join(TEST_BASE_DIR_NAME, f"thread_{worker_id}")
    thread_base_full = os.path.join(MOUNT_POINT, thread_base_rel)

    # Create the base directory for this thread's virtual items
    try:
        os.makedirs(thread_base_full, exist_ok=True)
    except OSError as e:
        logging.error(f"Worker {worker_id} failed to create its base directory {thread_base_full}: {e}")
        return 0, OPERATIONS_PER_THREAD # Assume all fail if base dir fails

    logging.info(f"Worker {worker_id} starting, base: {thread_base_full}")
    success_count = 0
    failure_count = 0
    random.seed(os.urandom(16) + str(worker_id).encode())

    # Define the mix of operations
    # Focus on structure/translation, less on pure reads/stats
    operations = [
        (create_virtual_dir_op, 3),       # High frequency
        (remove_virtual_dir_op, 1),       # Lower frequency, prone to expected failure
        (rename_virtual_dir_op, 2),       # Medium frequency
        (rename_physical_to_virtual_op, 2),# Medium frequency, tests translation creation
        (list_dir_op, 2),                 # Medium frequency read op
        (get_attrs_op, 1),                # Lower frequency read op
    ]
    op_funcs, op_weights = zip(*operations)
    total_weight = sum(op_weights)
    normalized_weights = [w / total_weight for w in op_weights]


    for i in range(OPERATIONS_PER_THREAD):
        # Choose operation based on weights
        op_func = random.choices(op_funcs, weights=normalized_weights, k=1)[0]

        result = False
        try:
            # Call the chosen operation function, passing the thread's base relative path
            result = op_func(thread_base_rel)

            if result:
                success_count += 1
            else:
                failure_count += 1
        except Exception as e:
            logging.exception(f"Unhandled exception in worker {worker_id} performing {op_func.__name__}: {e}")
            failure_count += 1

        time.sleep(random.uniform(0.01, 0.08)) # Slightly longer delays might reduce races slightly

    # --- Optional Cleanup ---
    # Try to remove tracked virtual dirs at the end (best effort)
    # final_dirs_to_remove = list(get_thread_virtual_dirs())
    # logging.info(f"Worker {worker_id} attempting cleanup of {len(final_dirs_to_remove)} tracked dirs.")
    # for vpath_rel in reversed(sorted(final_dirs_to_remove, key=len)): # Remove deepest first
    #     try:
    #         os.rmdir(os.path.join(MOUNT_POINT, vpath_rel))
    #         logging.debug(f"Cleanup removed: {vpath_rel}")
    #     except OSError:
    #         pass # Ignore errors during cleanup

    logging.info(f"Worker {worker_id} finished. Success: {success_count}, Failure/Warnings: {failure_count}")
    return success_count, failure_count

# --- Main Execution ---
if __name__ == "__main__":
    if not os.path.exists(MOUNT_POINT) or not os.path.ismount(MOUNT_POINT):
        logging.error(f"Mount point {MOUNT_POINT} does not exist or is not a mount point.")
        exit(1)

    # Ensure the main test base directory exists within the mount point
    main_test_dir_full = os.path.join(MOUNT_POINT, TEST_BASE_DIR_NAME)
    try:
        # Explicitly try to create the base "test" directory if needed.
        # Your FS needs to handle this (e.g., via create_virtual_dir or by it existing)
        os.makedirs(main_test_dir_full, exist_ok=True)
        logging.info(f"Ensured base test directory exists: {main_test_dir_full}")
    except OSError as e:
        # If the base "test" dir itself cannot be created/accessed, stop.
        logging.error(f"Failed to ensure base test directory {main_test_dir_full} exists: {e}")
        # Check if it's because it's a file or something unexpected
        if os.path.exists(main_test_dir_full) and not os.path.isdir(main_test_dir_full):
             logging.error(f"Path {main_test_dir_full} exists but is not a directory!")
        exit(1)


    logging.info(f"Starting focused stress test with {NUM_THREADS} threads, {OPERATIONS_PER_THREAD} ops each.")
    logging.info(f"Mount Point: {MOUNT_POINT}")
    logging.info(f"Test Base Dir Name: {TEST_BASE_DIR_NAME}")
    logging.info(f"Known physical items for translation test: {KNOWN_PHYSICAL_ITEMS_FULL}")

    total_success = 0
    total_failure = 0

    with ThreadPoolExecutor(max_workers=NUM_THREADS, thread_name_prefix="StressWorker") as executor:
        futures = [executor.submit(worker, i) for i in range(NUM_THREADS)]
        for future in as_completed(futures):
            try:
                s, f = future.result()
                total_success += s
                total_failure += f
            except Exception as e:
                logging.exception(f"Worker execution resulted in exception: {e}")
                total_failure += OPERATIONS_PER_THREAD # Assume all failed

    logging.info("Stress test finished.")
    logging.info(f"Total Successes: {total_success}")
    logging.info(f"Total Failures/Warnings: {total_failure}")
    logging.info(f"See {LOG_FILE} for detailed logs.")
