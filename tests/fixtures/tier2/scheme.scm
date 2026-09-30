(define-record-type circle
  (make-circle radius)
  circle?
  (radius circle-radius))

(define (square x)
  (* x x))

(define (area c)
  (* 3.14159 (square (circle-radius c))))

(define describe
  (lambda (c)
    (let ((value (area c)))
      (number->string value))))

(define-syntax twice
  (syntax-rules ()
    ((_ x) (* 2 x))))

(display (describe (make-circle 2)))
