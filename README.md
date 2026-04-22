# repro: vfio: system becomes unstable on sr-iov unbind

A system can be locked up when a bound virtual function is attempted
to be removed using `sriov_numvfs` by the physical function that is the
owner of a virtual function.

The `vfio-sriov-bind.py` script creates a VF and binds it to itself,
allowing reproduction of this bug by removing it:
- https://github.com/akamaxb/repro-vfio-sriov-removal.git

Run the `vfio-sriov-bind.py` script with the PF you want to add a VF to, 
and once that's set up you can check for the device what the `sriov_numvfs`
is and echo that `- 1` to the `sriov_numvfs` procfs file.
