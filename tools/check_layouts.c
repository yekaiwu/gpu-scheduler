/*
 * Dev-only: prints sizeof/offsetof for the kernel structures gsched reads through ctypes,
 * straight from the SDK headers. Compare with docs/implementation/02-platform.md section 4.
 *
 *   cc tools/check_layouts.c -o /tmp/cl && /tmp/cl
 */
#include <stddef.h>
#include <stdio.h>
#include <sys/sysctl.h>
#include <sys/resource.h>
#include <libproc.h>
#include <mach/mach.h>
#include <mach/vm_statistics.h>

#define OFF(t, f) printf("  offsetof(%s, %s) = %zu\n", #t, #f, offsetof(t, f))

int main(void) {
  printf("struct proc_bsdinfo: sizeof = %zu\n", sizeof(struct proc_bsdinfo));
  OFF(struct proc_bsdinfo, pbi_uid);
  OFF(struct proc_bsdinfo, pbi_comm);
  OFF(struct proc_bsdinfo, pbi_pgid);
  OFF(struct proc_bsdinfo, pbi_start_tvsec);
  OFF(struct proc_bsdinfo, pbi_start_tvusec);

  printf("struct rusage_info_v4: sizeof = %zu\n", sizeof(struct rusage_info_v4));
  OFF(struct rusage_info_v4, ri_phys_footprint);
  OFF(struct rusage_info_v4, ri_lifetime_max_phys_footprint);

  printf("struct vm_statistics64: sizeof = %zu, HOST_VM_INFO64_COUNT = %d\n",
         sizeof(struct vm_statistics64), (int)HOST_VM_INFO64_COUNT);
  OFF(struct vm_statistics64, free_count);
  OFF(struct vm_statistics64, wire_count);
  OFF(struct vm_statistics64, purgeable_count);
  OFF(struct vm_statistics64, speculative_count);
  OFF(struct vm_statistics64, compressor_page_count);
  OFF(struct vm_statistics64, external_page_count);
  OFF(struct vm_statistics64, internal_page_count);

  printf("struct xsw_usage: sizeof = %zu\n", sizeof(struct xsw_usage));
  OFF(struct xsw_usage, xsu_total);
  OFF(struct xsw_usage, xsu_avail);
  OFF(struct xsw_usage, xsu_used);
  return 0;
}
