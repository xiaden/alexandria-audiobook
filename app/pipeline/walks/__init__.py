"""Walk modules for the audiobook pipeline.

Each walk module exposes an ``execute(book_id, storage, config)`` function
that performs a discrete transformation step (e.g. scene segmentation,
character discovery). The WalkRunner in ``runner.py`` orchestrates serial
execution of these walks.
"""
