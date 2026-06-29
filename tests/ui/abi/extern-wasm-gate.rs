//! `extern "wasm"` is feature-gated behind `#![feature(abi_wasm)]` and is only a
//! supported ABI on wasm targets. This checks the gate fires when the feature is
//! not enabled, and that the ABI is rejected on non-wasm hosts.
// gate-test-abi_wasm
//@ revisions: host wasm
//@ add-minicore
//@ compile-flags: --crate-type=rlib
//@ [wasm] compile-flags: --target wasm32-unknown-unknown
//@ [wasm] needs-llvm-components: webassembly
//@ ignore-backends: gcc

#![feature(no_core, lang_items)]
#![no_core]

extern crate minicore;
use minicore::*;

// Function definition.
extern "wasm" fn f1() {} //~ ERROR "wasm" ABI is experimental and subject to change
//[host]~^ ERROR is not a supported ABI

// Function pointer type.
type A1 = extern "wasm" fn(); //~ ERROR "wasm" ABI is experimental and subject to change
//[host]~^ ERROR is not a supported ABI

// Foreign module.
extern "wasm" {} //~ ERROR "wasm" ABI is experimental and subject to change
//[host]~^ ERROR is not a supported ABI
