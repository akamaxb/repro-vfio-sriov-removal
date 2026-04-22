#!/usr/bin/env python3
"""
vfio-sriov-bind.py - Minimal reproducer for the kernel bug triggered when a
VFIO-bound SR-IOV VF is removed through sysfs while a KVM VM holds an MMU notifier.

Steps:
  1. Require sriov_numvfs == 0 on the PF (report any existing users and exit if not)
  2. Add one SR-IOV VF
  3. Bind the VF to vfio-pci via driver_override + drivers_probe
  4. Open VFIO container + group, get device fd
  5. Create a KVM VM (registers an MMU notifier — required to trigger the race)
  6. Hold and wait for user input

To trigger the bug while the script is waiting, in another terminal:
    echo 0 > /sys/bus/pci/devices/<pf_device>/sriov_numvfs
"""

import os
import struct
import fcntl
import ctypes
import argparse
import time

# VFIO ioctl numbers
def _IO(n): return (ord(';') << 8) | (100 + n)

VFIO_SET_IOMMU              = _IO(2)
VFIO_GROUP_GET_STATUS       = _IO(3)
VFIO_GROUP_SET_CONTAINER    = _IO(4)
VFIO_GROUP_GET_DEVICE_FD    = _IO(6)
VFIO_DEVICE_GET_REGION_INFO = _IO(8)
VFIO_IOMMU_MAP_DMA          = _IO(13)
VFIO_IOMMU_UNMAP_DMA        = _IO(14)

VFIO_TYPE1v2_IOMMU          = 3
VFIO_PCI_CONFIG_REGION_INDEX = 7

KVM_CREATE_VM = (0xae << 8) | 0x01

def parse_args():
    p = argparse.ArgumentParser(description="Minimal reproducer for VHP-1666 kernel bug.")
    p.add_argument("pf_device", help="PF PCI address (e.g. 0000:04:00.0)")
    p.add_argument("--no-kvm", action="store_true",
                   help="Skip KVM VM creation (MMU notifier won't be registered; "
                        "bug will not trigger)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# SR-IOV helpers
# ---------------------------------------------------------------------------

def get_current_driver(pci_addr):
    """Return the name of the driver bound to pci_addr, or None if unbound."""
    try:
        return os.path.basename(os.readlink(f"/sys/bus/pci/devices/{pci_addr}/driver"))
    except OSError:
        return None


def find_vf_users(pf_device, num_vfs):
    """
    Walk /proc/<pid>/fd for every running process and return a list of dicts
    describing any process that has a VFIO group fd open for one of the VFs.
    """
    users = []
    for vf_index in range(num_vfs):
        try:
            vf_addr = os.readlink(
                f"/sys/bus/pci/devices/{pf_device}/virtfn{vf_index}"
            ).split('/')[-1]
        except OSError:
            continue

        try:
            group = os.path.basename(
                os.readlink(f"/sys/bus/pci/devices/{vf_addr}/iommu_group")
            )
        except OSError:
            continue

        vfio_path = f"/dev/vfio/{group}"

        for pid_str in os.listdir("/proc"):
            if not pid_str.isdigit():
                continue
            fd_dir = f"/proc/{pid_str}/fd"
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue

            matching = [
                t for fd in fds
                for t in [os.readlink(f"{fd_dir}/{fd}") if True else ""]
                if t == vfio_path
            ]
            # ^^^ list comprehension gets messy; use a plain loop instead
            matching = []
            for fd in fds:
                try:
                    if os.readlink(f"{fd_dir}/{fd}") == vfio_path:
                        matching.append(vfio_path)
                except OSError:
                    pass

            if not matching:
                continue

            try:
                exe = os.readlink(f"/proc/{pid_str}/exe")
            except OSError:
                exe = "(unknown)"
            try:
                with open(f"/proc/{pid_str}/cmdline") as f:
                    cmdline = f.read().replace('\0', ' ').strip()
            except OSError:
                cmdline = "(unknown)"

            users.append({
                "vf_index": vf_index,
                "vf_addr":  vf_addr,
                "group":    group,
                "pid":      int(pid_str),
                "exe":      exe,
                "cmdline":  cmdline,
            })

    return users


def require_zero_vfs(pf_device):
    """
    Read sriov_numvfs. If it is already 0, return. Otherwise report which
    processes are using the existing VFs and exit with a non-zero status.
    """
    path = f"/sys/bus/pci/devices/{pf_device}/sriov_numvfs"
    with open(path) as f:
        current = int(f.read().strip())

    if current == 0:
        return

    print(f"[error] {pf_device} already has {current} VF(s). "
          f"sriov_numvfs must be 0 before we can add a new one.")
    print(f"[error] Kill the processes below, then run:")
    print(f"[error]     echo 0 > {path}")

    users = find_vf_users(pf_device, current)
    if not users:
        print("\n[info] No open VFIO fds found (VFs may be held by a kernel driver).")
    else:
        print(f"\n[info] {len(users)} process(es) with open VFIO fds:\n")
        for u in users:
            print(f"  virtfn{u['vf_index']} ({u['vf_addr']}) group {u['group']}")
            print(f"    pid:     {u['pid']}")
            print(f"    exe:     {u['exe']}")
            print(f"    cmdline: {u['cmdline']}")
            print()

    raise SystemExit(1)


def add_sriov_vf(pf_device):
    """Add one VF to pf_device (requires sriov_numvfs == 0). Returns the VF address."""
    path = f"/sys/bus/pci/devices/{pf_device}/sriov_numvfs"
    with open(path, 'w') as f:
        f.write("1\n")
    print(f"[setup] sriov_numvfs: 0 -> 1")

    vf_addr = os.readlink(f"/sys/bus/pci/devices/{pf_device}/virtfn0").split('/')[-1]
    print(f"[setup] VF address: {vf_addr}")
    return vf_addr


def remove_sriov_vfs(pf_device):
    """Set sriov_numvfs back to 0, destroying all VFs."""
    try:
        with open(f"/sys/bus/pci/devices/{pf_device}/sriov_numvfs", 'w') as f:
            f.write("0\n")
        print("[cleanup] sriov_numvfs -> 0")
    except OSError as e:
        print(f"[cleanup] failed to zero sriov_numvfs: {e}")


def bind_to_vfio_pci(vf_addr):
    """
    Bind vf_addr to vfio-pci using driver_override + drivers_probe.
    If already bound to vfio-pci, does nothing.
    Returns the original driver name (or None) so it can be restored on cleanup.
    """
    sysfs = f"/sys/bus/pci/devices/{vf_addr}"
    original = get_current_driver(vf_addr)
    print(f"[setup] {vf_addr}: current driver = {original or '(none)'}")

    if original == "vfio-pci":
        print(f"[setup] {vf_addr}: already bound to vfio-pci")
        return original

    if original:
        print(f"[setup] Unbinding {vf_addr} from {original} ...")
        with open(f"{sysfs}/driver/unbind", 'w') as f:
            f.write(vf_addr)
        time.sleep(0.2)

    print(f"[setup] Setting driver_override=vfio-pci on {vf_addr}")
    with open(f"{sysfs}/driver_override", 'w') as f:
        f.write("vfio-pci")

    print(f"[setup] Running drivers_probe for {vf_addr}")
    with open("/sys/bus/pci/drivers_probe", 'w') as f:
        f.write(vf_addr)

    bound = get_current_driver(vf_addr)
    if bound != "vfio-pci":
        raise RuntimeError(
            f"{vf_addr}: driver is {bound!r} after probe, expected vfio-pci. "
            "Is the module loaded?  Try: modprobe vfio-pci"
        )

    group = os.path.basename(os.readlink(f"{sysfs}/iommu_group"))
    vfio_group_path = f"/dev/vfio/{group}"
    print(f"[setup] Waiting for {vfio_group_path} ...")
    for _ in range(20):
        if os.path.exists(vfio_group_path):
            break
        time.sleep(0.1)
    else:
        raise RuntimeError(
            f"{vfio_group_path} did not appear after binding {vf_addr} to vfio-pci. "
            "Check: ls /dev/vfio/ and dmesg."
        )

    print(f"[setup] {vf_addr}: bound to vfio-pci (IOMMU group {group})")
    return original


def unbind_from_vfio_pci(vf_addr, original_driver):
    """
    Unbind vf_addr from vfio-pci, clear driver_override, scrub the vendor/device
    ID from vfio-pci's new_id table (in case an old run left it there), and
    rebind to the original driver.
    """
    sysfs = f"/sys/bus/pci/devices/{vf_addr}"

    # Scrub any leftover new_id entry so future VFs aren't auto-claimed by vfio-pci
    try:
        with open(f"{sysfs}/vendor") as f:
            vendor = f.read().strip()[2:]   # strip leading "0x"
        with open(f"{sysfs}/device") as f:
            dev_id = f.read().strip()[2:]
        with open("/sys/bus/pci/drivers/vfio-pci/remove_id", 'w') as f:
            f.write(f"{vendor} {dev_id}")
    except OSError:
        pass

    try:
        with open("/sys/bus/pci/drivers/vfio-pci/unbind", 'w') as f:
            f.write(vf_addr)
        print(f"[cleanup] Unbound {vf_addr} from vfio-pci")
    except OSError as e:
        print(f"[cleanup] Could not unbind {vf_addr} from vfio-pci: {e}")

    try:
        with open(f"{sysfs}/driver_override", 'w') as f:
            f.write("\n")
    except OSError:
        pass

    if original_driver and original_driver != "vfio-pci":
        try:
            with open(f"/sys/bus/pci/drivers/{original_driver}/bind", 'w') as f:
                f.write(vf_addr)
            print(f"[cleanup] Rebound {vf_addr} to {original_driver}")
        except OSError as e:
            print(f"[cleanup] Could not rebind {vf_addr} to {original_driver}: {e}")


# ---------------------------------------------------------------------------
# VFIO / KVM setup
# ---------------------------------------------------------------------------

def setup_vfio(vf_addr):
    """
    Open the VFIO container and group, associate them, set the IOMMU type,
    and obtain the device fd.  No group-viability checks — if the bind step
    above succeeded the group is viable.
    """
    group = os.path.basename(os.readlink(f"/sys/bus/pci/devices/{vf_addr}/iommu_group"))
    print(f"[setup] VFIO group {group}")

    container_fd = os.open("/dev/vfio/vfio", os.O_RDWR)
    group_fd     = os.open(f"/dev/vfio/{group}", os.O_RDWR)

    fcntl.ioctl(group_fd, VFIO_GROUP_SET_CONTAINER, struct.pack('i', container_fd))
    fcntl.ioctl(container_fd, VFIO_SET_IOMMU, VFIO_TYPE1v2_IOMMU)

    libc = ctypes.CDLL(None, use_errno=True)
    libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_char_p]
    libc.ioctl.restype  = ctypes.c_int
    device_fd = libc.ioctl(group_fd, VFIO_GROUP_GET_DEVICE_FD, (vf_addr + '\0').encode())
    if device_fd < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"VFIO_GROUP_GET_DEVICE_FD: {os.strerror(errno)}")

    print(f"[setup] VFIO fds — container={container_fd} group={group_fd} device={device_fd}")
    return container_fd, group_fd, device_fd


def setup_kvm():
    """
    Create a KVM VM.  Even with no memory regions, this registers an MMU notifier
    on the process address space — which is what makes the race in walk_pgd_range
    reachable.
    """
    kvm_fd = os.open("/dev/kvm", os.O_RDWR)
    vm_fd  = fcntl.ioctl(kvm_fd, KVM_CREATE_VM, 0)
    print(f"[setup] KVM VM created — kvm_fd={kvm_fd} vm_fd={vm_fd} (MMU notifier registered)")
    return kvm_fd, vm_fd


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    print(f"[setup] pid={os.getpid()}")

    require_zero_vfs(args.pf_device)
    vf_addr = add_sriov_vf(args.pf_device)
    original_driver = bind_to_vfio_pci(vf_addr)

    try:
        setup_vfio(vf_addr)

        if not args.no_kvm:
            setup_kvm()
        else:
            print("[setup] --no-kvm: skipping KVM VM creation (bug will not trigger)")

        print()
        print(f"[ready] Holding VFIO binding on {vf_addr}.")
        print(f"[ready] To trigger the bug, in another terminal run:")
        print(f"[ready]     echo 0 > /sys/bus/pci/devices/{args.pf_device}/sriov_numvfs")
        print(f"[ready] Press Enter or Ctrl-C to exit and clean up.")
        print()

        try:
            input()
        except KeyboardInterrupt:
            pass

    finally:
        print()
        unbind_from_vfio_pci(vf_addr, original_driver)
        remove_sriov_vfs(args.pf_device)
        print("[done]")


if __name__ == '__main__':
    main()
