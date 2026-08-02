// Kind-mapping coverage: enum and trait definitions (Package F1).
// Kept in a separate file from lib.rs/main.rs so this fixture addition
// cannot perturb the exact-set symbol assertions those two files
// already carry in test_extractor.py.

pub enum Shape {
    Circle,
    Square,
}

pub trait Named {
    fn name(&self) -> String;
}
