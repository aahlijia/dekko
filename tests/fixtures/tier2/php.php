<?php
namespace App;

interface Shape
{
    public function area(): float;
}

trait Named
{
    public function label(string $prefix): string
    {
        return $prefix . format_name($this->name);
    }
}

class Circle implements Shape
{
    use Named;

    public function __construct(private float $radius) {}

    public function area(): float
    {
        return pi() * square($this->radius);
    }
}

function square(float $x): float
{
    return $x * $x;
}

echo (new Circle(2.0))->area();
