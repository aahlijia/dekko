package shapes;

interface Shape {
	function area():Float;
}

typedef Point = {
	var x:Float;
}

class Circle implements Shape {
	var radius:Float;

	public function new(radius:Float) {
		this.radius = radius;
	}

	public function area():Float {
		return Math.PI * square(radius);
	}

	static function square(x:Float):Float {
		return x * x;
	}
}
