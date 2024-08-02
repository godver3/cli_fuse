import os

def full_path(root, partial):
    if partial.startswith("/"):
        partial = partial[1:]
    path = os.path.join(root, partial)
    return path

def should_hide(path, translations):
    """Check if the path should be hidden (i.e., it's an original file that has been translated)."""
    return path in translations or any(path.startswith(orig + '/') for orig in translations)

def add_virtual_dirs(virtual_dirs, path):
    while path != '/':
        if path not in virtual_dirs:
            virtual_dirs.add(path)
        path = os.path.dirname(path)

def remove_virtual_dirs(virtual_dirs, dir_structure, path):
    """Remove virtual directories that are no longer needed."""
    while path != '/':
        if not any(d.startswith(path + '/') for d in dir_structure):
            virtual_dirs.discard(path)
        else:
            break  # If this dir is still needed, its parents are too
        path = os.path.dirname(path)
