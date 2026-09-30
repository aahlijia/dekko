package shapes

trait Shape {
  def area: Double
}

class Circle(radius: Double) extends Shape {
  def area: Double = math.Pi * Circle.square(radius)
}

object Circle {
  def square(x: Double): Double = x * x
}

object Main {
  def main(args: Array[String]): Unit = println(new Circle(2).area)
}
