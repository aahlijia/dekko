use lib::{norm, Point};

fn main() {
    let p = Point::new(0.0, 1.0);
    let q = Point::new(3.0, 4.0);
    let d = p.dist(&q);
    println!("{}", d);
    let _ = norm(1.0, 2.0);
}
