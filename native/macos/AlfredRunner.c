/* Trusted fixed bootstrap: no agent data is read before the helper's boundary. */
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
#define ALFRED_LAUNCHER 1
#include "AlfredMachBoundary.h"

static int pipes_only(void) {
    struct stat input, output;
    return fstat(STDIN_FILENO, &input) == 0 && S_ISFIFO(input.st_mode)
        && fstat(STDOUT_FILENO, &output) == 0 && S_ISFIFO(output.st_mode);
}

int main(int argc, char **argv) {
    (void)argv;
    if (argc != 1 || !pipes_only()) return 64;
    char executable[PATH_MAX], resolved[PATH_MAX], helper[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) != 0
            || realpath(executable, resolved) == NULL) return 65;
    const char *suffix = "/Contents/MacOS/AlfredRunner";
    if (strlen(resolved) <= strlen(suffix)) return 65;
    size_t root = strlen(resolved) - strlen(suffix);
    if (strcmp(resolved + root, suffix) != 0) return 65;
    resolved[root] = '\0';
    if (snprintf(helper, sizeof(helper), "%s/Contents/Helpers/AlfredPython",
            resolved) >= (int)sizeof(helper)) return 65;
    /* Popen supplies clean FDs; CLOEXEC_DEFAULT repeats this for the child. */
    int nullfd = open("/dev/null", O_WRONLY);
    if (nullfd < 0 || dup2(nullfd, STDERR_FILENO) < 0) return 66;
    if (nullfd != STDERR_FILENO) close(nullfd);
    char *child_argv[] = {helper, NULL};
    char *child_env[] = {NULL};
    pid_t child;
    posix_spawnattr_t attributes;
    posix_spawn_file_actions_t actions;
    if (posix_spawnattr_init(&attributes)
            || posix_spawn_file_actions_init(&actions)) return 67;
    if (posix_spawnattr_setflags(&attributes, POSIX_SPAWN_CLOEXEC_DEFAULT)
            || posix_spawn_file_actions_addinherit_np(&actions, STDIN_FILENO)
            || posix_spawn_file_actions_addinherit_np(&actions, STDOUT_FILENO)
            || posix_spawn_file_actions_addinherit_np(&actions, STDERR_FILENO)) return 67;
    if (!alfred_prepare_mach_child(&attributes)) return 74;
    int spawned = posix_spawn(&child, helper, &actions, &attributes,
                             child_argv, child_env);
    posix_spawn_file_actions_destroy(&actions);
    posix_spawnattr_destroy(&attributes);
    if (spawned != 0) return 67;
    close(STDIN_FILENO);
    close(STDOUT_FILENO);
    int status;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) return 68;
    }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    return 69;
}
