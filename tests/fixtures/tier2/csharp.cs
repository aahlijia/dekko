namespace Shapes
{
    public interface IShape
    {
        double Area();
    }

    public struct Point
    {
        public double X;
    }

    public class Circle : IShape
    {
        private readonly double radius;

        public Circle(double radius)
        {
            this.radius = Guard(radius);
        }

        public double Area()
        {
            return Math.PI * Square(radius);
        }

        private static double Square(double x) => x * x;

        private static double Guard(double x) => x;
    }
}
