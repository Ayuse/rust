// `extern "wasm"` (Component Model Canonical ABI) lowering, spike in progress:
// arguments are flattened to core values, and a multi-value result travels
// through a return-area pointer passed as the *last* argument (canonical),
// rather than the wasm C-ABI's return-pointer-first sret convention.

//@ add-minicore
//@ compile-flags: --target wasm32-unknown-unknown -C no-prepopulate-passes
//@ needs-llvm-components: webassembly

#![crate_type = "lib"]
#![feature(no_core, lang_items, abi_wasm)]
#![no_core]

extern crate minicore;
use minicore::*;

// Scalar return: a single core value, always Direct (never sret).
// CHECK: define {{.*}}i32 @scalar_ret(i32 {{.*}})
// CHECK-NOT: sret
#[no_mangle]
pub extern "wasm" fn scalar_ret(x: i32) -> i32 {
    x
}

// `(i32, i32)` argument: flattened to two core i32 values (canonical).
// CHECK: define {{.*}}void @pair_arg(i32 {{.*}}, i32 {{.*}})
#[no_mangle]
pub extern "wasm" fn pair_arg(_x: (i32, i32)) {}

// `&str` argument: flattened to a `(ptr, len)` pair of core values (canonical).
// CHECK: define {{.*}}void @str_arg(ptr {{.*}}, i32 {{.*}})
#[no_mangle]
pub extern "wasm" fn str_arg(_s: &str) {}

// `&str` return: a two-value result cannot be returned directly (the canonical
// ABI allows at most one core result value), so it travels through a return-area
// pointer. That pointer is the *last* argument, after the flattened `(ptr, len)`
// argument — the canonical return-area-last convention (not sret-first). The
// callee writes the returned `(ptr, len)` into that trailing return area.
// CHECK: define {{.*}}void @str_ret(ptr {{.*}}, i32 {{.*}}, ptr {{.*}}sret{{.*}})
#[no_mangle]
pub extern "wasm" fn str_ret(s: &str) -> &str {
    s
}
