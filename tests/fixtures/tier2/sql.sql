CREATE TABLE "shapes" (
    id INT PRIMARY KEY,
    radius REAL
);

CREATE INDEX idx_shapes_radius ON shapes (radius);

CREATE OR REPLACE FUNCTION square(x REAL) RETURNS REAL AS $$
    SELECT x * x
$$ LANGUAGE sql;

INSERT INTO shapes (id, radius) VALUES (1, 2.0);

SELECT id, square(radius) FROM shapes;
