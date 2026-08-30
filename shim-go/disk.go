package main

import "syscall"

// diskAvailBytes reports bytes available to an unprivileged process (avail) and
// the volume's total size (total) for the filesystem backing path. ok=false when
// the volume cannot be stat'd, letting the caller disable the disk-free floor
// rather than act on bad numbers. Bsize is int64 on Linux and uint32 on Darwin;
// the int64 conversions cover both.
func diskAvailBytes(path string) (avail, total int64, ok bool) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return 0, 0, false
	}
	return int64(st.Bavail) * int64(st.Bsize), int64(st.Blocks) * int64(st.Bsize), true
}
