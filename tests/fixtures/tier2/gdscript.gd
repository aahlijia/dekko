class_name Shapes
extends Node

class Circle:
	var radius := 0.0

	func _init(r):
		radius = r

	func area():
		return PI * Shapes.square(radius)

static func square(x):
	return x * x

func _ready():
	print(Circle.new(2.0).area())
