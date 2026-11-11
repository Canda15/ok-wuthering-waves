/* 使用 CPython 稳定接口在自身进程执行业务，避免再启动 pythonw.exe。
 * 宿主随部署树更新，Python 和第三方依赖仍由现有启动器维护。
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <strsafe.h>
#include <stdlib.h>
#include <wchar.h>

#define PATH_CAPACITY 32768

typedef int (__cdecl *PythonMain)(int, wchar_t **);

static int startup_error(const wchar_t *message)
{
    HANDLE output = GetStdHandle(STD_ERROR_HANDLE);
    char text[2048];
    DWORD written;
    int size = WideCharToMultiByte(CP_UTF8, 0, message, -1, text, sizeof(text), NULL, NULL);

    /* 启动器会捕获标准错误；直接双击时才需要可见的错误提示。 */
    if (size > 0 && output != NULL && output != INVALID_HANDLE_VALUE &&
        WriteFile(output, text, (DWORD)(size - 1), &written, NULL)) {
        return 1;
    }
    MessageBoxW(NULL, message, L"NoNo", MB_OK | MB_ICONERROR);
    return 1;
}

static BOOL python_invocation(const wchar_t *argument)
{
    const wchar_t *options[] = {
        L"-c", L"-m", L"-u", L"-B", L"-E", L"-I", L"-O", L"-OO",
        L"-s", L"-S", L"-W", L"-X", L"-V", L"--version"
    };
    size_t length = wcslen(argument);
    size_t i;

    if (length >= 3 && _wcsicmp(argument + length - 3, L".py") == 0) {
        return TRUE;
    }
    for (i = 0; i < _countof(options); ++i) {
        if (wcscmp(argument, options[i]) == 0) {
            return TRUE;
        }
    }
    return FALSE;
}

int WINAPI wWinMain(_In_ HINSTANCE instance, _In_opt_ HINSTANCE previous,
                   _In_ PWSTR command, _In_ int show)
{
    /* 路径缓冲区不占用线程栈，把栈空间留给 Python 和第三方原生模块。 */
    static wchar_t project[PATH_CAPACITY];
    static wchar_t candidate[PATH_CAPACITY];
    static wchar_t runtime[PATH_CAPACITY];
    static wchar_t library_path[PATH_CAPACITY];
    static wchar_t script[PATH_CAPACITY];
    wchar_t *separator;
    wchar_t **arguments;
    wchar_t **python_arguments;
    HMODULE library;
    PythonMain python_main;
    DWORD length;
    int argument_count;
    int result;
    int i;

    UNREFERENCED_PARAMETER(instance);
    UNREFERENCED_PARAMETER(previous);
    UNREFERENCED_PARAMETER(command);
    UNREFERENCED_PARAMETER(show);

    length = GetModuleFileNameW(NULL, project, _countof(project));
    if (length == 0 || length >= _countof(project)) {
        return startup_error(L"无法定位程序目录，请重新解压完整的 NoNo 便携包。");
    }
    /* 文件位于 working\selfhost\NoNo.exe；从自身位置定位，兼容快捷方式和不同工作目录。 */
    separator = wcsrchr(project, L'\\');
    if (separator == NULL) {
        return startup_error(L"程序目录结构不完整，请重新解压完整的 NoNo 便携包。");
    }
    *separator = L'\0';
    separator = wcsrchr(project, L'\\');
    if (separator == NULL) {
        return startup_error(L"程序目录结构不完整，请重新解压完整的 NoNo 便携包。");
    }
    *separator = L'\0';

    if (!SetCurrentDirectoryW(project)) {
        return startup_error(L"无法进入程序目录，请重新解压完整的 NoNo 便携包。");
    }
    if (FAILED(StringCchPrintfW(candidate, _countof(candidate), L"%s\\..\\python", project)) ||
        FAILED(StringCchPrintfW(script, _countof(script), L"%s\\main.py", project))) {
        return startup_error(L"程序路径过长，请把 NoNo 便携包解压到较短的目录。");
    }
    length = GetFullPathNameW(candidate, _countof(runtime), runtime, NULL);
    if (length == 0 || length >= _countof(runtime) ||
        FAILED(StringCchPrintfW(library_path, _countof(library_path), L"%s\\python3.dll", runtime))) {
        return startup_error(L"无法定位程序运行时，请重新解压完整的 NoNo 便携包。");
    }

    /* 明确使用便携包的运行时和源码，防止直接启动快捷方式时混入系统 Python。 */
    if (_wputenv_s(L"PYTHONHOME", runtime) != 0 ||
        _wputenv_s(L"PYTHONPATH", project) != 0 ||
        _wputenv_s(L"PYTHONNOUSERSITE", L"1") != 0 ||
        _wputenv_s(L"PYTHONIOENCODING", L"utf-8") != 0) {
        return startup_error(L"无法初始化程序环境，请关闭后重新启动 NoNo。");
    }
    library = LoadLibraryExW(library_path, NULL,
                            LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (library == NULL) {
        return startup_error(L"无法加载程序运行时，请重新解压完整的 NoNo 便携包，勿单独复制 EXE。");
    }
    /* GetProcAddress 返回通用函数指针；Py_Main 的签名由 CPython 稳定 ABI 保证。 */
#pragma warning(push)
#pragma warning(disable: 4191)
    python_main = (PythonMain)GetProcAddress(library, "Py_Main");
#pragma warning(pop)
    if (python_main == NULL) {
        return startup_error(L"程序运行时不兼容，请重新下载完整的 NoNo 便携包。");
    }

    arguments = CommandLineToArgvW(GetCommandLineW(), &argument_count);
    if (arguments == NULL) {
        return startup_error(L"无法读取启动参数，请关闭后重新启动 NoNo。");
    }
    /* 计划任务、提权重启及子进程继续兼容 sys.executable main.py / -m / -c。
     * 普通启动则自动加入 main.py，应用的 -t、-e、-h 等参数保持原样。
     */
    if (argument_count > 1 && python_invocation(arguments[1])) {
        result = python_main(argument_count, arguments);
    } else {
        python_arguments = LocalAlloc(LMEM_FIXED, ((size_t)argument_count + 2) * sizeof(wchar_t *));
        if (python_arguments == NULL) {
            LocalFree(arguments);
            return startup_error(L"内存不足，无法启动 NoNo。");
        }
        python_arguments[0] = arguments[0];
        python_arguments[1] = script;
        for (i = 1; i < argument_count; ++i) {
            python_arguments[i + 1] = arguments[i];
        }
        python_arguments[argument_count + 1] = NULL;
        result = python_main(argument_count + 1, python_arguments);
        LocalFree(python_arguments);
    }
    LocalFree(arguments);
    /* 与标准 pythonw.exe 一样，让操作系统在进程退出时卸载运行时。 */
    return result;
}
