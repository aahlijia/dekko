module Shapes

abstract type Shape end

struct Circle <: Shape
    radius::Float64
end

square(x) = x * x

function area(c::Circle)::Float64
    return pi * square(c.radius)
end

Base.show(io::IO, c::Circle) = print(io, area(c))

end

println(Shapes.area(Shapes.Circle(2.0)))
