pub struct S {
    a: Entity<Foo>,
    pub b: Vec<u8>,
}

struct N(Foo, pub u32);

enum E {
    V { inner: Foo },
}

mod tests {
    struct S {
        t: Test,
    }
}
