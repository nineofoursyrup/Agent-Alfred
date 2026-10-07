"""Offline mocked Mach return states, never a native isolation certificate."""

import subprocess
import sys
from pathlib import Path

import pytest

C_FIXTURE = r'''
#include <mach/mach.h>
#include <mach/exception_types.h>
#include <mach/task_special_ports.h>
#include <mach/mach_traps.h>
#include <mach_debug/ipc_info.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static mach_port_name_t names[] = {513};
static mach_port_type_t types[] = {MACH_PORT_TYPE_RECEIVE};
static mach_port_status_t attributes;
static mach_port_t fixture_bootstrap = MACH_PORT_NULL;
static mach_port_t fixture_debug = MACH_PORT_NULL;
static kern_return_t debug_status = KERN_SUCCESS;
static kern_return_t debug_release_status = KERN_SUCCESS;
static mach_port_rights_t sender_count = 0;
static kern_return_t sender_status = KERN_SUCCESS;
static unsigned sender_calls = 0;

static kern_return_t fixture_names(ipc_space_t task,
        mach_port_name_array_t *out, mach_msg_type_number_t *count,
        mach_port_type_array_t *kind, mach_msg_type_number_t *kind_count) {
    (void)task; *out = names; *count = 1; *kind = types; *kind_count = 1;
    return KERN_SUCCESS;
}
static kern_return_t fixture_attributes(ipc_space_t task, mach_port_name_t name,
        mach_port_flavor_t flavor, mach_port_info_t out,
        mach_msg_type_number_t *count) {
    (void)task; (void)name; (void)flavor;
    memcpy(out, &attributes, sizeof(attributes));
    *count = MACH_PORT_RECEIVE_STATUS_COUNT; return KERN_SUCCESS;
}
static kern_return_t fixture_srights(ipc_space_t task, mach_port_name_t name,
        mach_port_rights_t *out) {
    (void)task; (void)name; sender_calls++;
    if (sender_status == KERN_SUCCESS) *out = sender_count;
    return sender_status;
}
static kern_return_t fixture_deallocate(ipc_space_t task, mach_port_name_t name) {
    (void)task;
    return name == fixture_debug ? debug_release_status : KERN_SUCCESS;
}
static kern_return_t fixture_vm(vm_map_t map, vm_address_t address, vm_size_t size) {
    (void)map; (void)address; (void)size; return KERN_SUCCESS;
}
static kern_return_t fixture_special(task_t task, int which, mach_port_t *out) {
    (void)task;
    if (which == TASK_DEBUG_CONTROL_PORT) {
        *out = fixture_debug; return debug_status;
    }
    *out = MACH_PORT_NULL; return KERN_SUCCESS;
}
static kern_return_t fixture_threads(task_t task, thread_act_array_t *out,
        mach_msg_type_number_t *count) {
    (void)task; static thread_t thread[] = {100};
    *out = thread; *count = 1; return KERN_SUCCESS;
}
static kern_return_t fixture_registered(task_t task, mach_port_array_t *out,
        mach_msg_type_number_t *count) {
    (void)task; static mach_port_t empty[MACH_PORTS_SLOTS_USED] = {0};
    *out = empty; *count = MACH_PORTS_SLOTS_USED; return KERN_SUCCESS;
}
static kern_return_t fixture_task_exceptions(task_t task, exception_mask_t mask,
        exception_mask_array_t masks, mach_msg_type_number_t *count,
        exception_handler_array_t ports, exception_behavior_array_t behaviors,
        exception_flavor_array_t flavors) {
    (void)task; (void)behaviors; (void)flavors;
    masks[0] = mask; ports[0] = MACH_PORT_NULL; *count = 1; return KERN_SUCCESS;
}
static kern_return_t fixture_thread_exceptions(thread_t task, exception_mask_t mask,
        exception_mask_array_t masks, mach_msg_type_number_t *count,
        exception_handler_array_t ports, exception_behavior_array_t behaviors,
        exception_flavor_array_t flavors) {
    (void)task; (void)mask; (void)masks; (void)ports; (void)behaviors; (void)flavors;
    *count = 0; return KERN_SUCCESS;
}
static kern_return_t fixture_dyld(mach_port_t *ports, mach_msg_type_number_t *count) {
    (void)ports; *count = 0; return KERN_SUCCESS;
}

#undef mach_task_self
#undef mach_thread_self
#define mach_task_self() ((mach_port_t)101)
#define mach_thread_self() ((mach_port_t)100)
#define bootstrap_port fixture_bootstrap
#define mach_port_names fixture_names
#define mach_port_get_attributes fixture_attributes
#define mach_port_get_srights fixture_srights
#define mach_port_deallocate fixture_deallocate
#define vm_deallocate fixture_vm
#define task_get_special_port fixture_special
#define task_threads fixture_threads
#define mach_ports_lookup fixture_registered
#define task_get_exception_ports fixture_task_exceptions
#define thread_get_exception_ports fixture_thread_exceptions
#define task_dyld_process_info_notify_get fixture_dyld
#include "AlfredMachBoundary.h"

int main(int argc, char **argv) {
    if (argc != 2) return 64;
    int kind = atoi(argv[1]);
    switch (kind) {
    case 0: break;
    case 1: attributes.mps_sorights = 1; break;
    case 2: attributes.mps_nsrequest = TRUE; break;
    case 3: attributes.mps_pdrequest = TRUE; break;
    case 4: attributes.mps_msgcount = 1; break;
    case 5: types[0] |= MACH_PORT_TYPE_DNREQUEST; break;
    case 6: types[0] |= MACH_PORT_TYPE_SPREQUEST; break;
    case 7: types[0] |= MACH_PORT_TYPE_SPREQUEST_DELAYED; break;
    case 8: types[0] |= MACH_PORT_TYPE_PORT_SET; break;
    case 9: case 10: case 12:
        types[0] |= MACH_PORT_TYPE_SEND;
        attributes.mps_srights = TRUE;
        sender_count = kind == 10 ? 2 : 1;
        if (kind == 12) attributes.mps_srights = FALSE;
        break;
    case 11: sender_status = KERN_NO_ACCESS; break;
    case 13: attributes.mps_srights = TRUE; sender_count = 1; break;
    case 21: fixture_debug = 999; break;
    case 14: case 15: case 16: case 17: case 18: case 19: case 20:
        types[0] |= MACH_PORT_TYPE_SEND;
        attributes.mps_srights = TRUE;
        attributes.mps_flags = MACH_PORT_STATUS_FLAG_GUARDED
            | MACH_PORT_STATUS_FLAG_STRICT_GUARD;
        fixture_debug = names[0]; sender_count = 2;
        if (kind == 15) fixture_debug = 999;
        if (kind == 16) sender_count = 3;
        if (kind == 17) attributes.mps_flags = 0;
        if (kind == 18) debug_status = KERN_NO_ACCESS;
        if (kind == 19) sender_status = KERN_NO_ACCESS;
        if (kind == 20) debug_release_status = KERN_NO_ACCESS;
        break;
    default: return 64;
    }
    int birth = alfred_mach_child_birth_is_clean();
    unsigned birth_calls = sender_calls;
    int confined = alfred_no_external_send_rights();
    printf("%d %d %u %u\n", birth, confined, birth_calls, sender_calls);
    return 0;
}
'''


@pytest.fixture(scope="module")
def receive_guard(tmp_path_factory):
    if sys.platform != "darwin":
        pytest.skip("mocked C boundary uses the macOS SDK declarations")
    directory = tmp_path_factory.mktemp("mocked-mach-receive")
    source, binary = directory / "fixture.c", directory / "fixture"
    source.write_text(C_FIXTURE)
    native = Path(__file__).resolve().parents[4] / "native" / "macos"
    subprocess.run(
        ["/usr/bin/clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
         "-Wno-unused-function", "-I", str(native), str(source), "-o", str(binary)],
        check=True, capture_output=True, env={}, timeout=20,
    )

    def observe(kind):
        result = subprocess.run(
            [str(binary), str(kind)], check=True, capture_output=True,
            text=True, env={}, timeout=5,
        )
        return tuple(map(int, result.stdout.split()))

    return observe


@pytest.mark.parametrize("kind", [1, 2, 3, 4, 5, 6, 7, 8, 12, 13])
def test_receive_with_foreign_or_notification_metadata_stops(receive_guard, kind):
    birth, confined, _, _ = receive_guard(kind)
    assert birth == 0
    assert confined == 0


@pytest.mark.parametrize("kind", [0, 9])
def test_only_consistent_owned_receivers_pass_birth(receive_guard, kind):
    birth, confined, before_calls, after_calls = receive_guard(kind)
    assert birth == confined == 1
    assert before_calls == after_calls == 1


@pytest.mark.parametrize("kind", [10, 11])
def test_birth_requires_observed_exact_sender_count(receive_guard, kind):
    birth, confined, before_calls, after_calls = receive_guard(kind)
    assert birth == 0
    assert before_calls == after_calls == 1
    # The confined snapshot cannot invoke denied MIG3222. Its local metadata
    # result alone never certifies that a later foreign sender is absent.
    assert confined == 1


def test_own_strict_guarded_kernel_debug_slot_passes_birth(receive_guard):
    birth, confined, before_calls, after_calls = receive_guard(14)
    assert birth == confined == 1
    assert before_calls == after_calls == 1


@pytest.mark.parametrize("kind", [15, 16, 17, 18, 19, 20, 21])
def test_debug_slot_does_not_whitelist_unknown_two_senders(receive_guard, kind):
    birth, confined, _, _ = receive_guard(kind)
    assert birth == 0
    assert confined == 1
