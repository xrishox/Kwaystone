/* Redirect WebKitGTK helper-process paths into the AppImage bundle.
 *
 * Release builds of WebKitGTK compile in an absolute helper directory (for
 * the Debian bundle: /usr/lib/x86_64-linux-gnu/webkitgtk-6.0) and ignore
 * WEBKIT_EXEC_PATH, so WebKitNetworkProcess/WebKitWebProcess can never be
 * found on hosts with a different layout; the UI process then aborts with a
 * fatal g_error (SIGTRAP), taking the whole app down. This LD_PRELOAD shim
 * rewrites exec/spawn/dlopen of paths under WAYSTONE_WEBKIT_EXEC_SRC to the
 * bundled directory in WAYSTONE_WEBKIT_EXEC_REDIRECT. It is inert when the
 * variables are unset or the path does not match.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *redirect(const char *path, char *buf, size_t n) {
    const char *src = getenv("WAYSTONE_WEBKIT_EXEC_SRC");
    const char *target = getenv("WAYSTONE_WEBKIT_EXEC_REDIRECT");
    if (!src || !target || !path)
        return path;
    size_t len = strlen(src);
    if (len == 0 || strncmp(path, src, len) != 0)
        return path;
    if ((size_t)snprintf(buf, n, "%s/%s", target, path + len) >= n)
        return path;
    return buf;
}

int execve(const char *path, char *const argv[], char *const envp[]) {
    static int (*real)(const char *, char *const[], char *const[]);
    if (!real)
        real = dlsym(RTLD_NEXT, "execve");
    char buf[4096];
    return real(redirect(path, buf, sizeof buf), argv, envp);
}

int posix_spawn(pid_t *pid, const char *path,
                const posix_spawn_file_actions_t *fa,
                const posix_spawnattr_t *attr,
                char *const argv[], char *const envp[]) {
    static int (*real)(pid_t *, const char *, const posix_spawn_file_actions_t *,
                       const posix_spawnattr_t *, char *const[], char *const[]);
    if (!real)
        real = dlsym(RTLD_NEXT, "posix_spawn");
    char buf[4096];
    return real(pid, redirect(path, buf, sizeof buf), fa, attr, argv, envp);
}

void *dlopen(const char *path, int flags) {
    static void *(*real)(const char *, int);
    if (!real)
        real = dlsym(RTLD_NEXT, "dlopen");
    char buf[4096];
    return real(redirect(path, buf, sizeof buf), flags);
}
