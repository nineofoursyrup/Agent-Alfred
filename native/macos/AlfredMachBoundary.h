/* Fixed, single-threaded native startup before Python or any agent input.
 * No namespace destruction: unexpected capabilities stop initialization.
 */
#ifndef ALFRED_MACH_BOUNDARY_H
#define ALFRED_MACH_BOUNDARY_H
#include <mach/mach.h>
#include <mach/exception_types.h>
#include <mach/task_special_ports.h>
#include <mach/mach_traps.h>
#include <mach_debug/ipc_info.h>
#include <spawn.h>

#define ALFRED_EXCEPTION_MASK (EXC_MASK_ALL | EXC_MASK_CRASH | EXC_MASK_CORPSE_NOTIFY)

static int alfred_registered_empty(void) {
    mach_port_array_t ports = NULL;
    mach_msg_type_number_t count = 0;
    if (mach_ports_lookup(mach_task_self(), &ports, &count) != KERN_SUCCESS)
        return 0;
    int empty = count == MACH_PORTS_SLOTS_USED;
    for (mach_msg_type_number_t i = 0; i < count; i++) {
        if (ports[i] != MACH_PORT_NULL) empty = 0;
        if (MACH_PORT_VALID(ports[i])) {
            if (mach_port_deallocate(mach_task_self(), ports[i]) != KERN_SUCCESS)
                empty = 0;
        }
    }
    if (ports && vm_deallocate(mach_task_self(), (vm_address_t)ports,
                              count * sizeof(mach_port_t)) != KERN_SUCCESS)
        return 0;
    return empty;
}

static int alfred_exceptions_empty(int thread) {
    exception_mask_t masks[EXC_TYPES_COUNT];
    mach_port_t ports[EXC_TYPES_COUNT];
    exception_behavior_t behaviors[EXC_TYPES_COUNT];
    thread_state_flavor_t flavors[EXC_TYPES_COUNT];
    mach_msg_type_number_t count = EXC_TYPES_COUNT;
    mach_port_t target = thread ? mach_thread_self() : mach_task_self();
    kern_return_t status = thread
        ? thread_get_exception_ports(target, ALFRED_EXCEPTION_MASK, masks,
                                     &count, ports, behaviors, flavors)
        : task_get_exception_ports(target, ALFRED_EXCEPTION_MASK, masks,
                                   &count, ports, behaviors, flavors);
    if (thread && mach_port_deallocate(mach_task_self(), target) != KERN_SUCCESS)
        return 0;
    if (status != KERN_SUCCESS || count > EXC_TYPES_COUNT) return 0;
    /* XNU returns SUCCESS/count=0 when a thread has no exception actions. */
    if (thread && count == 0) return 1;
    int empty = 1;
    exception_mask_t covered = 0;
    for (mach_msg_type_number_t i = 0; i < count; i++) {
        covered |= masks[i];
        if (ports[i] != MACH_PORT_NULL) empty = 0;
        if (MACH_PORT_VALID(ports[i])
                && mach_port_deallocate(mach_task_self(), ports[i]) != KERN_SUCCESS)
            empty = 0;
    }
    return empty && (covered & ALFRED_EXCEPTION_MASK) == ALFRED_EXCEPTION_MASK;
}

#ifdef ALFRED_LAUNCHER
static int alfred_prepare_mach_child(posix_spawnattr_t *attributes) {
    if (mach_ports_register(mach_task_self(), NULL, 0) != KERN_SUCCESS
            || !alfred_registered_empty()) return 0;
    if (posix_spawnattr_setspecialport_np(attributes, MACH_PORT_NULL,
                                         TASK_BOOTSTRAP_PORT) != 0) return 0;
    if (posix_spawnattr_setexceptionports_np(attributes, ALFRED_EXCEPTION_MASK,
            MACH_PORT_NULL, EXCEPTION_DEFAULT, THREAD_STATE_NONE) != 0) return 0;
    /* A parent's per-thread exception right must not reach the new thread. */
    mach_port_t thread = mach_thread_self();
    kern_return_t cleared = thread_set_exception_ports(
        thread, ALFRED_EXCEPTION_MASK, MACH_PORT_NULL,
        EXCEPTION_DEFAULT, THREAD_STATE_NONE);
    kern_return_t released = mach_port_deallocate(mach_task_self(), thread);
    return cleared == KERN_SUCCESS && released == KERN_SUCCESS
        && alfred_exceptions_empty(1);
}
#else
typedef struct {
    const char *check;
    kern_return_t status;
    /* 0 unknown; 1 status and port observation; 2 status, no port observation. */
    int has_status;
    int returned_nonnull;
} AlfredMachFailure;

static int alfred_mach_failed(AlfredMachFailure *failure, const char *check,
        int has_status, kern_return_t status, int returned_nonnull) {
    if (failure && !failure->check) {
        failure->check = check;
        failure->has_status = has_status;
        failure->status = status;
        failure->returned_nonnull = returned_nonnull;
    }
    return 0;
}

static int alfred_no_external_send_rights_mode(AlfredMachFailure *failure,
                                              int check_global_senders);

static int alfred_single_thread(void) {
    thread_act_array_t threads = NULL;
    mach_msg_type_number_t count = 0;
    if (task_threads(mach_task_self(), &threads, &count) != KERN_SUCCESS) return 0;
    int okay = count == 1;
    for (mach_msg_type_number_t i = 0; i < count; i++)
        if (mach_port_deallocate(mach_task_self(), threads[i]) != KERN_SUCCESS)
            okay = 0;
    if (threads && vm_deallocate(mach_task_self(), (vm_address_t)threads,
                                 count * sizeof(*threads)) != KERN_SUCCESS)
        okay = 0;
    return okay;
}

static int alfred_dyld_notifiers_are_empty(void) {
    /* These observers are held by the kernel, outside mach_port_names().
     * Birth must observe SUCCESS/count=0; an error's unchanged capacity is
     * never an empty-array witness. Returned rights are only released. */
    mach_port_t ports[32] = {0};
    mach_msg_type_number_t count = 32;
    kern_return_t status = task_dyld_process_info_notify_get(ports, &count);
    int empty = status == KERN_SUCCESS && count == 0;
    for (unsigned i = 0; i < 32; i++) {
        if (ports[i] != MACH_PORT_NULL) empty = 0;
        if (MACH_PORT_VALID(ports[i])) {
            if (mach_port_deallocate(mach_task_self(), ports[i]) != KERN_SUCCESS)
                empty = 0;
        }
    }
    return empty;
}

static int alfred_mach_child_birth_is_clean(void) {
    /* No cached bootstrap alias is permitted either. ACCESS is not replaced:
     * XNU forbids that on this OS. Its getter is blocked by the final policy. */
    mach_port_t port = MACH_PORT_NULL;
    kern_return_t status = task_get_special_port(mach_task_self(),
                                                 TASK_BOOTSTRAP_PORT, &port);
    int empty = status == KERN_SUCCESS && port == MACH_PORT_NULL
        && bootstrap_port == MACH_PORT_NULL;
    if (MACH_PORT_VALID(port)) mach_port_deallocate(mach_task_self(), port);
    return empty && alfred_single_thread() && alfred_registered_empty()
        && alfred_exceptions_empty(0) && alfred_exceptions_empty(1)
        && alfred_dyld_notifiers_are_empty()
        && alfred_no_external_send_rights_mode(NULL, 1);
}

static int alfred_no_external_send_rights_mode(AlfredMachFailure *failure,
                                              int check_global_senders) {
    mach_port_t debug_receive = MACH_PORT_NULL;
    if (check_global_senders) {
        /* The trusted kernel retains a SEND for this own-task slot. Obtain
         * its identity only before policy/input; release the returned SEND
         * before counting. This does not accept arbitrary guarded ports. */
        kern_return_t observed = task_get_special_port(mach_task_self(),
                                      TASK_DEBUG_CONTROL_PORT, &debug_receive);
        kern_return_t released = KERN_SUCCESS;
        if (MACH_PORT_VALID(debug_receive))
            released = mach_port_deallocate(mach_task_self(), debug_receive);
        if (observed != KERN_SUCCESS)
            return alfred_mach_failed(failure, "namespace_debug_slot_call", 2,
                                      observed, 0);
        if (released != KERN_SUCCESS)
            return alfred_mach_failed(failure, "namespace_debug_slot_release", 2,
                                      released, 0);
    }
    mach_port_name_array_t names = NULL;
    mach_port_type_array_t types = NULL;
    mach_msg_type_number_t count = 0, type_count = 0;
    mach_port_t thread = mach_thread_self();
    kern_return_t status = mach_port_names(mach_task_self(), &names, &count,
                                          &types, &type_count);
    int okay = status == KERN_SUCCESS && count == type_count && count <= 128;
    if (!okay) alfred_mach_failed(failure, "namespace_list", 2, status, 0);
    int debug_seen = debug_receive == MACH_PORT_NULL;
    unsigned semaphores = 0, clocks = 0;
    for (mach_msg_type_number_t i = 0; okay && i < count; i++) {
        if (types[i] & MACH_PORT_TYPE_SEND_ONCE) {
            okay = 0;
            alfred_mach_failed(failure, "namespace_send_once", 0, 0, 0);
            break;
        }
        if (types[i] & MACH_PORT_TYPE_RECEIVE) {
            mach_port_type_t allowed = MACH_PORT_TYPE_RECEIVE;
            int local_send = (types[i] & MACH_PORT_TYPE_SEND) != 0;
            if (local_send) allowed |= MACH_PORT_TYPE_SEND;
            if (types[i] != allowed) {
                okay = 0;
                alfred_mach_failed(failure, "namespace_receive_type", 0, 0, 0);
                break;
            }
            mach_port_status_t receive;
            mach_msg_type_number_t size = MACH_PORT_RECEIVE_STATUS_COUNT;
            kern_return_t received = mach_port_get_attributes(mach_task_self(), names[i],
                    MACH_PORT_RECEIVE_STATUS, (mach_port_info_t)&receive, &size);
            if (received != KERN_SUCCESS) {
                okay = 0;
                alfred_mach_failed(failure, "namespace_receive_call", 2, received, 0);
            } else if (size != MACH_PORT_RECEIVE_STATUS_COUNT) {
                okay = 0;
                alfred_mach_failed(failure, "namespace_receive_size", 0, 0, 0);
            } else if (receive.mps_msgcount != 0) {
                okay = 0;
                alfred_mach_failed(failure, "namespace_receive_queue", 0, 0, 0);
            } else if (receive.mps_sorights != 0 || receive.mps_nsrequest
                       || receive.mps_pdrequest
                       || !!receive.mps_srights != local_send) {
                okay = 0;
                alfred_mach_failed(failure, "namespace_receive_rights", 0, 0, 0);
            } else if (check_global_senders) {
                /* Before policy/Python/input, the listed local SEND is one
                 * global right regardless of urefs. Only the pinned own
                 * DEBUG_CONTROL slot can account for a second kernel-held
                 * SEND, with the observed strict-guarded metadata. Other
                 * count2/extra/unknown states fail. This is a trusted-kernel,
                 * single-threaded birth snapshot, not a lifetime census.
                 * Final policy denies both debug getters and count MIG;
                 * confined checks retain only local metadata checks. */
                mach_port_rights_t expected = (mach_port_rights_t)local_send;
                if (names[i] == debug_receive) {
                    debug_seen = 1;
                    if (!local_send || receive.mps_flags
                            != (MACH_PORT_STATUS_FLAG_GUARDED
                                | MACH_PORT_STATUS_FLAG_STRICT_GUARD)) {
                        okay = 0;
                        alfred_mach_failed(failure, "namespace_debug_slot_metadata",
                                          0, 0, 0);
                        continue;
                    }
                    expected++;
                }
                mach_port_rights_t senders = 0;
                kern_return_t counted = mach_port_get_srights(mach_task_self(),
                                                              names[i], &senders);
                if (counted != KERN_SUCCESS) {
                    okay = 0;
                    alfred_mach_failed(failure, "namespace_receive_srights_call",
                                      2, counted, 0);
                } else if (senders != expected) {
                    okay = 0;
                    alfred_mach_failed(failure, "namespace_receive_srights_count",
                                      0, 0, 0);
                }
            }
            continue;
        }
        if (types[i] != MACH_PORT_TYPE_SEND) {
            okay = 0;
            alfred_mach_failed(failure, "namespace_type", 0, 0, 0);
            break;
        }
        natural_t kind = IPC_OTYPE_UNKNOWN;
        mach_vm_address_t address = 0;
        kobject_description_t description = {0};
        kern_return_t described = mach_port_kobject_description(mach_task_self(), names[i],
                &kind, &address, description);
        if (described != KERN_SUCCESS) {
            okay = 0;
            alfred_mach_failed(failure, "namespace_kobject", 2, described, 0);
            break;
        }
        /* Addresses/descriptions never leave trusted C. Types alone do not
         * prove origin/ownership or a read-only clock. At most one existing
         * semaphore/clock is tolerated. Final MIG rules also permit the two
         * clock/semaphore factories needed by libc atfork. Unlisted MIG
         * routines and Mach traps remain denied; this is not proof that
         * every use path is denied (BSD semwait paths are unverified).
         * The clock service is a kernel service, not a read-only object.
         * This count is a startup/explicit witness check, not a continuous
         * limit on later creation. Types alone still prove no ownership. */
        if (names[i] == mach_task_self() && kind == IPC_OTYPE_TASK_CONTROL) continue;
        if (names[i] == thread && kind == IPC_OTYPE_THREAD_CONTROL) continue;
        if (kind == IPC_OTYPE_HOST) continue; /* Never HOST_PRIV. */
        if (kind == IPC_OTYPE_SEMAPHORE && ++semaphores == 1) continue;
        if (kind == IPC_OTYPE_CLOCK && ++clocks == 1) continue;
        okay = 0;
        alfred_mach_failed(failure, "namespace_object", 0, 0, 0);
    }
    if (okay && check_global_senders && !debug_seen) {
        okay = 0;
        alfred_mach_failed(failure, "namespace_debug_slot_receiver", 0, 0, 0);
    }
    if (names) {
        kern_return_t released = vm_deallocate(mach_task_self(), (vm_address_t)names,
                                               count * sizeof(*names));
        if (released != KERN_SUCCESS) {
            okay = 0;
            alfred_mach_failed(failure, "namespace_names_release", 2, released, 0);
        }
    }
    if (types) {
        kern_return_t released = vm_deallocate(mach_task_self(), (vm_address_t)types,
                                               type_count * sizeof(*types));
        if (released != KERN_SUCCESS) {
            okay = 0;
            alfred_mach_failed(failure, "namespace_types_release", 2, released, 0);
        }
    }
    kern_return_t released = mach_port_deallocate(mach_task_self(), thread);
    if (released != KERN_SUCCESS) {
        okay = 0;
        alfred_mach_failed(failure, "namespace_thread_release", 2, released, 0);
    }
    return okay;
}

static int alfred_no_external_send_rights_with_failure(AlfredMachFailure *failure) {
    return alfred_no_external_send_rights_mode(failure, 0);
}

static int alfred_no_external_send_rights(void) {
    return alfred_no_external_send_rights_with_failure(NULL);
}

static int alfred_host_io_getter_is_confined(void) {
    /* SDK API/MIG205 is host_get_io_main. This fixed OS's SBPL compiler
     * retains the host_get_io_master spelling; the policy must deny it.
     * An ordinary HOST right is otherwise sufficient to acquire I/O access. */
    mach_port_t host = mach_host_self();
    mach_port_t io = MACH_PORT_NULL;
    kern_return_t status = host_get_io_main(host, &io);
    int denied = status == KERN_DENIED && io == MACH_PORT_NULL;
    if (MACH_PORT_VALID(io)
            && mach_port_deallocate(mach_task_self(), io) != KERN_SUCCESS)
        denied = 0;
    if (mach_port_deallocate(mach_task_self(), host) != KERN_SUCCESS) denied = 0;
    return denied;
}

static int alfred_mach_child_is_confined_with_failure(AlfredMachFailure *failure) {
    mach_port_t port = MACH_PORT_NULL;
    kern_return_t status = task_get_special_port(mach_task_self(),
                                                 TASK_ACCESS_PORT, &port);
    int denied = status == KERN_DENIED && port == MACH_PORT_NULL;
    if (MACH_PORT_VALID(port)) mach_port_deallocate(mach_task_self(), port);
    if (!denied) return alfred_mach_failed(failure, "task_access", 1,
                                         status, port != MACH_PORT_NULL);
    port = MACH_PORT_NULL;
    status = debug_control_port_for_pid(mach_task_self(), getpid(), &port);
    denied = status == KERN_DENIED && port == MACH_PORT_NULL;
    if (MACH_PORT_VALID(port)) mach_port_deallocate(mach_task_self(), port);
    if (!denied) return alfred_mach_failed(failure, "debug_pid", 1,
                                         status, port != MACH_PORT_NULL);
    port = MACH_PORT_NULL;
    status = task_name_for_pid(mach_task_self(), getpid(), &port);
    denied = status == KERN_DENIED && port == MACH_PORT_NULL;
    if (MACH_PORT_VALID(port)) mach_port_deallocate(mach_task_self(), port);
    if (!denied) return alfred_mach_failed(failure, "name_pid", 1,
                                         status, port != MACH_PORT_NULL);
    port = MACH_PORT_NULL;
    status = task_for_pid(mach_task_self(), getpid(), &port);
    denied = status == KERN_DENIED && port == MACH_PORT_NULL;
    if (MACH_PORT_VALID(port)) mach_port_deallocate(mach_task_self(), port);
    if (!denied) return alfred_mach_failed(failure, "control_pid", 1,
                                         status, port != MACH_PORT_NULL);
    mach_port_t observers[32] = {0};
    mach_msg_type_number_t observer_count = 32;
    status = task_dyld_process_info_notify_get(observers, &observer_count);
    denied = status == KERN_DENIED;
    int observers_nonnull = 0;
    for (unsigned i = 0; i < 32; i++) {
        if (observers[i] != MACH_PORT_NULL) observers_nonnull = 1;
        if (observers[i] != MACH_PORT_NULL) denied = 0;
        if (MACH_PORT_VALID(observers[i])
                && mach_port_deallocate(mach_task_self(), observers[i])
                    != KERN_SUCCESS) denied = 0;
    }
    if (!denied) return alfred_mach_failed(failure, "dyld_observers", 1,
                                         status, observers_nonnull);
    if (!alfred_registered_empty())
        return alfred_mach_failed(failure, "registered", 0, 0, 0);
    if (!alfred_exceptions_empty(0))
        return alfred_mach_failed(failure, "task_exceptions", 0, 0, 0);
    if (!alfred_exceptions_empty(1))
        return alfred_mach_failed(failure, "thread_exceptions", 0, 0, 0);
    if (!alfred_host_io_getter_is_confined())
        return alfred_mach_failed(failure, "host_io", 0, 0, 0);
    if (!alfred_single_thread())
        return alfred_mach_failed(failure, "single_thread", 0, 0, 0);
    if (!alfred_no_external_send_rights_with_failure(failure)) return 0;
    return 1;
}

static int alfred_mach_child_is_confined(void) {
    return alfred_mach_child_is_confined_with_failure(NULL);
}
#endif
#endif
