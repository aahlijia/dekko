module Shapes
  abstract class Shape
    abstract def area : Float64
  end

  struct Point
    getter x : Float64 = 0.0
  end

  class Circle < Shape
    def initialize(@radius : Float64)
    end

    def area : Float64
      Math::PI * Shapes.square(@radius)
    end
  end

  def self.square(x : Float64) : Float64
    x * x
  end
end

puts Shapes::Circle.new(2.0).area
