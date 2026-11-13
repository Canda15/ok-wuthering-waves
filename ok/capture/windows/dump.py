import sys
import threading
import traceback

from ok.util.file import ensure_dir_for_file, get_user_data_path


def get_thread_name(thread_id):
    for thread in threading.enumerate():
        if thread.ident == thread_id:
            return thread.name
    return ""  # Return empty string if the thread name is not found


def dump_threads():
    # Get the stack trace for each thread
    thread_dumps = []
    for thread_id, frame in sys._current_frames().items():
        thread_name = get_thread_name(thread_id)
        thread_dump = f"Stack for thread {thread_id} (Name: {thread_name}):\n"
        thread_dump += "".join(traceback.format_stack(frame))
        thread_dumps.append(thread_dump)

    # Write the thread dumps to a file
    output_file = get_user_data_path('logs', 'thread_dumps.txt')
    ensure_dir_for_file(output_file)
    print('正在保存线程诊断')

    with open(output_file, "w", encoding='utf-8') as f:
        f.write("\n\n".join(thread_dumps))
