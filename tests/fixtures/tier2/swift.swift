protocol Shape {
    func area() -> Double
}

struct Circle: Shape {
    let radius: Double

    func area() -> Double {
        return Double.pi * square(radius)
    }
}

func square(_ x: Double) -> Double {
    return x * x
}

let circle = Circle(radius: 2)
print(circle.area())
