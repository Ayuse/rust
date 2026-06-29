//! `#[repr(wasm)]` is feature-gated behind `#![feature(repr_wasm)]` and only
//! applies to structs (Component Model record types). This checks the gate
//! fires without the feature, and that the attribute is accepted with it.
// gate-test-repr_wasm
//@ revisions: ungated gated
//@ [gated] check-pass
#![cfg_attr(gated, feature(repr_wasm))]

// Without the feature gate this should error.
#[cfg(ungated)]
#[repr(wasm)] //[ungated]~ ERROR `#[repr(wasm)]` is experimental
struct Ungated {
    x: i32,
}

// With the feature gate, a struct is accepted.
#[cfg(gated)]
#[repr(wasm)]
struct Point {
    x: f32,
    y: f32,
}

fn main() {}
