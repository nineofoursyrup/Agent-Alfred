/* Bundled libPython: isolated configuration without ambient import locations. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <ffi/ffi.h>
#include <fcntl.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include <sandbox.h>
#include <arpa/inet.h>
#include <errno.h>
#include <signal.h>
#include "AlfredMachBoundary.h"

static void checked(PyStatus status) {
    if (PyStatus_Exception(status)) Py_ExitStatusException(status);
}

/* Failure-only fixed metadata, before Python or any input. Never an approval
 * or success frame. Ordinary write failures retain exit 78; process termination
 * and incomplete frames remain unknown to the host. */
static void emit_boundary_failure(const AlfredMachFailure *failure) {
    struct sigaction action = {0};
    action.sa_handler = SIG_IGN;
    if (sigemptyset(&action.sa_mask) || sigaction(SIGPIPE, &action, NULL)) return;
    char raw_status[32], returned[8], frame[512];
    if (failure->has_status) {
        snprintf(raw_status, sizeof(raw_status), "%d", failure->status);
        snprintf(returned, sizeof(returned), "%s",
                 failure->has_status == 2 ? "null" :
                 failure->returned_nonnull ? "true" : "false");
    } else {
        strcpy(raw_status, "null");
        strcpy(returned, "null");
    }
    int size = snprintf(frame, sizeof(frame),
        "{\"contract\":\"V1-LOCAL-NATIVE-BOUNDARY-FAILURE\",\"version\":2,"
        "\"pid\":%d,\"ppid\":%d,\"uid\":%u,\"check\":\"%s\","
        "\"raw_status\":%s,\"returned_nonnull\":%s}",
        getpid(), getppid(), getuid(), failure->check, raw_status, returned);
    if (size <= 0 || (size_t)size >= sizeof(frame)) return;
    uint32_t length = htonl((uint32_t)size);
    const void *parts[] = {&length, frame};
    size_t sizes[] = {sizeof(length), (size_t)size};
    for (unsigned i = 0; i < 2; i++) {
        const char *data = parts[i];
        size_t left = sizes[i];
        while (left) {
            ssize_t written = write(1, data, left);
            if (written < 0 && errno == EINTR) continue;
            if (written <= 0) return;
            data += written;
            left -= (size_t)written;
        }
    }
}

/* Fixed metadata observation only. No port name, address, service selection,
 * argument or native invocation capability is exposed to Python. The guard is
 * unchanged; these observations do not relax it or grant product admission. */
static PyObject *boundary_witness(PyObject *self, PyObject *ignored) {
    (void)self;
    (void)ignored;
    int single = alfred_single_thread();
    int registered = alfred_registered_empty();
    int task_exceptions = alfred_exceptions_empty(0);
    int thread_exceptions = alfred_exceptions_empty(1);
    int names = alfred_no_external_send_rights();
    int confined = alfred_mach_child_is_confined();
    int cached = bootstrap_port == MACH_PORT_NULL;
    return Py_BuildValue("{s:O,s:O,s:O,s:O,s:O,s:O,s:O}",
        "single_thread", single ? Py_True : Py_False,
        "registered_empty", registered ? Py_True : Py_False,
        "task_exceptions_empty", task_exceptions ? Py_True : Py_False,
        "thread_exceptions_empty", thread_exceptions ? Py_True : Py_False,
        "namespace_confined", names ? Py_True : Py_False,
        "existing_guard_confined", confined ? Py_True : Py_False,
        "bootstrap_cache_empty", cached ? Py_True : Py_False);
}

static PyMethodDef witness_methods[] = {
    {"witness", boundary_witness, METH_NOARGS, "Observe the fixed native guard."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef witness_module = {
    PyModuleDef_HEAD_INIT, "_alfred_boundary_witness", NULL, -1,
    witness_methods, NULL, NULL, NULL, NULL
};

static PyObject *init_boundary_witness(void) {
    return PyModule_Create(&witness_module);
}

static void set_path(PyConfig *config, wchar_t **field,
        const char *root, const char *tail) {
    char path[PATH_MAX];
    if (snprintf(path, sizeof(path), "%s/%s", root, tail) >= (int)sizeof(path))
        exit(65);
    checked(PyConfig_SetBytesString(config, field, path));
}

static void add_path(PyConfig *config, const char *root, const char *tail) {
    char path[PATH_MAX];
    if (snprintf(path, sizeof(path), "%s/%s", root, tail) >= (int)sizeof(path))
        exit(65);
    wchar_t *wide = Py_DecodeLocale(path, NULL);
    if (!wide) exit(65);
    checked(PyWideStringList_Append(&config->module_search_paths, wide));
    PyMem_RawFree(wide);
}

/* Fixed trusted initialization, before confinement or Python/agent input.
 * libffi intentionally retains its first trampoline table after this free.
 * No callback is prepared/invoked and no worker-selectable setup is exposed.
 * Once confined, cache exhaustion must fail without a permission fallback. */
static int initialize_fixed_system_ffi(void) {
    void *code = NULL;
    ffi_closure *owned = ffi_closure_alloc(sizeof(ffi_closure), &code);
    int ready = owned != NULL && code != NULL;
    if (owned) {
        ffi_closure *release = owned;
        owned = NULL;
        ffi_closure_free(release);
    }
    return ready;
}

int main(int argc, char **argv) {
    (void)argv;
    struct stat input, output;
    if (argc != 1 || fstat(0, &input) || !S_ISFIFO(input.st_mode)
            || fstat(1, &output) || !S_ISFIFO(output.st_mode)) return 64;
    if (!alfred_mach_child_birth_is_clean()) return 75;
    if (!initialize_fixed_system_ffi()) return 80;
    /* Recheck new native effects, including hidden dyld observers. Do not
     * destroy unexpected capabilities or equate denied export with emptiness. */
    if (!alfred_mach_child_birth_is_clean()) return 75;
    /* Native parent used CLOEXEC_DEFAULT and inherited only descriptors 0..2. */
    char executable[PATH_MAX], resolved[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size)
            || !realpath(executable, resolved)) return 65;
    const char *suffix = "/Contents/Helpers/AlfredPython";
    if (strlen(resolved) <= strlen(suffix)) return 65;
    size_t root = strlen(resolved) - strlen(suffix);
    if (strcmp(resolved + root, suffix)) return 65;
    resolved[root] = '\0';
    /* Only this immutable, host-verified profile is read before confinement.
     * No Python, agent frame, path, argument or environment selects a policy. */
    char policy_path[PATH_MAX], policy[65537];
    if (snprintf(policy_path, sizeof(policy_path),
            "%s/Contents/Resources/runner.sb", resolved)
            >= (int)sizeof(policy_path)) return 65;
    int policy_fd = open(policy_path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
    struct stat policy_stat;
    if (policy_fd < 0 || fstat(policy_fd, &policy_stat)
            || !S_ISREG(policy_stat.st_mode) || policy_stat.st_size <= 0
            || policy_stat.st_size > 65536) return 76;
    size_t used = 0;
    while (used < (size_t)policy_stat.st_size) {
        ssize_t got = read(policy_fd, policy + used, policy_stat.st_size - used);
        if (got <= 0) return 76;
        used += (size_t)got;
    }
    if (close(policy_fd) || memchr(policy, '\0', used)) return 76;
    policy[used] = '\0';
    char *policy_error = NULL;
    /* Deprecated raw SBPL is the explicitly approved fixed-OS boundary. */
    if (sandbox_init(policy, 0, &policy_error) != 0) {
        if (policy_error) sandbox_free_error(policy_error);
        return 77;
    }
    AlfredMachFailure boundary_failure = {0};
    if (!alfred_mach_child_is_confined_with_failure(&boundary_failure)) {
        emit_boundary_failure(&boundary_failure);
        return 78;
    }
    if (PyImport_AppendInittab("_alfred_boundary_witness", init_boundary_witness))
        return 79;
    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    config.use_environment = 0;
    config.site_import = 0;
    config.user_site_directory = 0;
    config.write_bytecode = 0;
    config.safe_path = 1;
    config.parse_argv = 0;
    config.module_search_paths_set = 1;
    set_path(&config, &config.home, resolved, "Contents/Resources/python");
    set_path(&config, &config.program_name, resolved,
             "Contents/Helpers/AlfredPython");
    set_path(&config, &config.executable, resolved,
             "Contents/Helpers/AlfredPython");
    set_path(&config, &config.run_filename, resolved,
             "Contents/Resources/runner.py");
    add_path(&config, resolved, "Contents/Resources/python/lib/python3.14");
    add_path(&config, resolved,
             "Contents/Resources/python/lib/python3.14/lib-dynload");
    add_path(&config, resolved, "Contents/Resources/site-packages");
    add_path(&config, resolved, "Contents/Resources/packages");
    checked(Py_InitializeFromConfig(&config));
    PyConfig_Clear(&config);
    return Py_RunMain();
}
