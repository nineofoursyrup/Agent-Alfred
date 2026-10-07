"""Fixed custom Seatbelt policy; owner-approved, OS-pinned and fail-closed.

Apple does not publicly support third-party SBPL. This adapter is the explicitly
accepted local boundary change, not App Sandbox or an isolation PASS by itself.
"""

import json
import os
import subprocess
from pathlib import Path

CONTRACT = "V1-LOCAL-CUSTOM-SEATBELT"
LOADER = "Contents/Helpers/AlfredPython:sandbox_init-before-PyConfig"
PROFILE_PATH = "Contents/Resources/runner.sb"


def system_identity():
    """Bind the OS; the native policy loader's bytes are in the bundle inventory."""
    result = subprocess.run(
        ["/usr/bin/sw_vers", "-buildVersion"],
        capture_output=True,
        text=True,
        env={},
        close_fds=True,
        check=True,
        timeout=10,
    )
    build = result.stdout.strip()
    if not build or len(build) > 64 or any(c.isspace() for c in build):
        raise ValueError("local_sandbox_os_identity_unverifiable")
    return {
        "os_build": build,
        "kernel_release": os.uname().release,
        "architecture": os.uname().machine,
        "kernel_version": os.uname().version,
    }


def boundary_contract():
    return {
        "contract": CONTRACT,
        "version": 3,
        "profile": PROFILE_PATH,
        "loader": LOADER,
        "system": system_identity(),
    }


def verify_boundary(value):
    if (
        type(value) is not dict
        or type(value.get("version")) is not int
        or value != boundary_contract()
    ):
        raise ValueError("local_sandbox_boundary_changed")


def _path(value):
    path = Path(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or any(ord(c) < 32 for c in str(path))
        or "\\" in str(path)
    ):
        raise ValueError("local_sandbox_path_invalid")
    return path


def runner_profile(app, state):
    """Bind one immutable app and one disposable case; no broad user-directory IO.

    Metadata for the two exact ancestor chains supports realpath/dyld traversal.
    It does not permit enumerating their contents or reading sibling files.
    Root-directory data access is required by this OS's dyld startup; it is a
    literal rule, never a recursive root grant. Native initialization is trusted;
    this complete policy is entered once before PyConfig or any input frame.
    """
    app, state = _path(app), _path(state)
    if app == state or app in state.parents or state in app.parents:
        raise ValueError("local_sandbox_code_state_overlap")
    def quote(path):
        return json.dumps(str(path), ensure_ascii=False)
    ancestors = sorted(set(app.parents) | set(state.parents), key=str)
    metadata = "\n ".join(f"(literal {quote(path)})" for path in ancestors)
    return f'''(version 1)
(deny default)
(deny mach-task-special-port*)
(deny syscall-mig)
(allow syscall-mig
 (kernel-mig-routine
  mach_port_names task_threads_from_user mach_ports_lookup
  task_get_exception_ports_from_user thread_get_exception_ports_from_user
  mach_port_kobject_description_from_user mach_port_get_attributes_from_user
  host_get_clock_service semaphore_create))
(deny syscall-mach)
(allow syscall-mach
 (machtrap-number
  MSC__kernelrpc_mach_port_allocate_trap
  MSC__kernelrpc_mach_port_construct_trap
  MSC__kernelrpc_mach_port_deallocate_trap
  MSC__kernelrpc_mach_port_destruct_trap
  MSC__kernelrpc_mach_port_get_attributes_trap
  MSC__kernelrpc_mach_port_guard_trap
  MSC__kernelrpc_mach_port_insert_member_trap
  MSC__kernelrpc_mach_port_insert_right_trap
  MSC__kernelrpc_mach_port_mod_refs_trap
  MSC__kernelrpc_mach_port_type_trap
  MSC__kernelrpc_mach_vm_allocate_trap
  MSC__kernelrpc_mach_vm_deallocate_trap
  MSC__kernelrpc_mach_vm_map_trap
  MSC__kernelrpc_mach_vm_protect_trap
  MSC__kernelrpc_mach_vm_purgable_control_trap
  MSC_host_self_trap MSC_task_self_trap MSC_thread_self_trap
  MSC_mach_reply_port MSC_thread_get_special_reply_port
  MSC_mach_msg2_trap MSC_mach_timebase_info_trap MSC_syscall_thread_switch))
(allow process-fork)
(allow process-info* (target self))
(allow signal (target self) (target children))
(allow sysctl-read
 (sysctl-name "hw.ncpu") (sysctl-name "hw.activecpu")
 (sysctl-name "hw.physicalcpu") (sysctl-name "hw.logicalcpu")
 (sysctl-name "hw.pagesize") (sysctl-name "hw.memsize")
 (sysctl-name "hw.optional.arm64") (sysctl-name "kern.osrelease")
 (sysctl-name "kern.ostype") (sysctl-name "kern.osversion")
 (sysctl-name "kern.hostname") (sysctl-name "kern.version")
 (sysctl-name "kern.osrevision") (sysctl-name "hw.machine"))
(allow file-read-metadata
 {metadata})
(allow file-read* (literal "/")
 (subpath "/System/Library") (subpath "/usr/lib")
 (subpath "/Library/Apple/System/Library")
 (literal "/dev/null") (literal "/dev/urandom")
 (subpath {quote(app)}))
(allow file-write-data (literal "/dev/null"))
(allow file-read* file-write* (subpath {quote(state)}))
'''


def launch_arguments(app):
    app = _path(app)
    return [str(app / "Contents/MacOS/AlfredRunner")]
