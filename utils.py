import os

def full_path(root, path):
    """
    Joins the root directory with the relative FUSE path.

    Args:
        root: The absolute path to the underlying filesystem root.
        path: The relative path within the FUSE filesystem (potentially translated).

    Returns:
        The absolute path on the underlying filesystem.
    """
    # Remove leading slash from path if present, as os.path.join handles it
    path_relative = path.lstrip('/')
    return os.path.join(root, path_relative)

def add_virtual_dirs(virtual_dirs_set, translated_path):
    """
    Adds all parent directories of a translated path to the virtual directory set.

    Args:
        virtual_dirs_set: A set containing paths of virtual directories.
        translated_path: The translated file or directory path.
    """
    parent = os.path.dirname(translated_path)
    while parent and parent != '/':
        if parent not in virtual_dirs_set:
            # print(f"Adding virtual dir: {parent}") # Debugging
            virtual_dirs_set.add(parent)
        else:
            # If parent already exists, all its ancestors also exist
            break
        parent = os.path.dirname(parent)

def remove_virtual_dirs(virtual_dirs_set, dir_structure, removed_dir_path):
    """
    Removes a directory and potentially its parents from the virtual directory set
    if they are no longer needed (i.e., contain no other translated items).

    Args:
        virtual_dirs_set: The set of virtual directory paths.
        dir_structure: The dictionary mapping directories to sets of their translated children.
        removed_dir_path: The directory path potentially being removed from virtual status.
    """
    parent = removed_dir_path
    while parent and parent != '/':
        # Check if this directory still holds other translated items OR is itself a translated target
        # This check is simplified: assumes dir_structure accurately reflects needed dirs.
        # A more robust check might involve reverse_translations lookup too.
        if parent not in dir_structure and parent in virtual_dirs_set:
            # print(f"Removing virtual dir: {parent}") # Debugging
            virtual_dirs_set.discard(parent)
            parent = os.path.dirname(parent)
        else:
            # Stop removing parents if the current one is still needed
            break

def should_hide(fuse_path, translations):
    """
    Checks if a path (potentially representing an original file/dir)
    should be hidden because its original counterpart has been translated.

    NOTE: This function is complex to get right based only on fuse_path.
    readdir ideally needs to know the *original* path for an entry to check
    if that original path exists as a key in `translations`.
    This implementation is a placeholder approximation.

    Args:
        fuse_path: The path as seen by the FUSE system.
        translations: The dictionary mapping original paths to translated paths.

    Returns:
        True if the item corresponding to fuse_path might be an original
        that has been translated elsewhere, False otherwise.
    """
    # This is difficult. We don't easily know the 'original' path from the fuse_path
    # without doing a reverse lookup or passing more context.
    # A simple (and potentially incorrect) heuristic:
    # If the fuse_path *itself* exists as an 'original' key in translations,
    # it means it *should* have been renamed, so hide this representation.
    # This doesn't cover cases where a parent dir was renamed.
    # The readdir logic needs to be the primary driver for hiding.
    # return fuse_path in translations # Very basic check

    # Returning False makes readdir simpler (it just lists everything it finds
    # from physical + virtual, letting duplicates potentially appear if not handled there)
    return False
