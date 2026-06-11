package demo;

public class App {
    private final Helper helper = new Helper();

    public static void main(String[] args) {
        App app = new App();
        app.run(args.length);
    }

    public int run(int count) {
        return helper.twice(count);
    }
}

class Helper {
    public int twice(int x) {
        return x + x;
    }
}
