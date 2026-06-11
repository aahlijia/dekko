pub struct Point {
    x: f64,
    y: f64,
}

impl Point {
    pub fn new(x: f64, y: f64) -> Self {
        Self { x, y }
    }

    pub fn dist(&self, other: &Point) -> f64 {
        norm(self.x - other.x, self.y - other.y)
    }
}

pub fn norm(a: f64, b: f64) -> f64 {
    (a * a + b * b).sqrt()
}
