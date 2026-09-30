module shapes;

import std.stdio;

interface Shape
{
    double area();
}

struct Point
{
    double x;
}

class Circle : Shape
{
    double radius;

    double area()
    {
        return 3.14159 * square(radius);
    }
}

double square(double x)
{
    return x * x;
}

void main()
{
    auto circle = new Circle();
    writeln(circle.area());
}
