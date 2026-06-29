// `extern "wasm"` (Component Model Canonical ABI) lowering, spike in progress:
// arguments are flattened to core values (canonical), but returns are still the
// wasm C-ABI sret (return pointer first, not the canonical return-area-last).
// The CHECK lines record the current state so a future return fix trips them.

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

// `&str` return: currently sret with the return pointer FIRST, followed by the
// flattened `(ptr, len)` argument. Canonical ABI wants the return pointer LAST
// (see file header) — recorded here as the known-divergent current state.
// CHECK: define {{.*}}void @str_ret(ptr {{.*}}sret{{.*}}, ptr {{.*}}, i32 {{.*}})
#[no_mangle]
pub extern "wasm" fn str_ret(s: &str) -> &str {
    s
}
