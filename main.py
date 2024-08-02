import os
import logging
from fuse import FUSE
from translation_fs import TranslationFS
from api import run_flask
import threading

logging.basicConfig(filename='fuse_translation.log', level=logging.DEBUG,
                    format='%(asctime)s - %(levelname)s - %(message)s')

def main(mountpoint, root, db_file, backup_dir):
    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir)

    if os.path.exists(db_file):
        if not TranslationFS.check_db_integrity(db_file):
            logging.error("Database integrity check failed. Please check the database file.")
            return
    else:
        logging.info(f"Database file {db_file} does not exist. A new one will be created.")

    global fuse_fs
    fuse_fs = TranslationFS(root, db_file, backup_dir)

    # Start Flask API in a separate thread
    api_thread = threading.Thread(target=run_flask, args=(fuse_fs,))
    api_thread.start()

    logging.info(f"Mounting at {mountpoint}, root: {root}, database file: {db_file}")
    FUSE(fuse_fs, mountpoint, nothreads=True, foreground=True, ro=True)

if __name__ == '__main__':
    import sys
    if len(sys.argv) != 5:
        print('Usage: %s <mountpoint> <root> <db_file> <backup_dir>' % sys.argv[0])
        sys.exit(1)
    mountpoint = sys.argv[1]
    root = sys.argv[2]
    db_file = sys.argv[3]
    backup_dir = sys.argv[4]
    main(mountpoint, root, db_file, backup_dir)
