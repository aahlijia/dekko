#lang racket

(struct circle (radius))

(define (square x)
  (* x x))

(define (area c)
  (* pi (square (circle-radius c))))

(define describe
  (lambda (c)
    (let ([value (area c)])
      (number->string value))))

(displayln (describe (circle 2)))
