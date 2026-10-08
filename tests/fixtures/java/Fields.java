class Fields {
    private final Foo x = new Foo();
    Bar y, z;

    record R(Foo a, int b) {}

    enum E { A, B }

    void m() {
        Foo local = new Foo();
    }
}
