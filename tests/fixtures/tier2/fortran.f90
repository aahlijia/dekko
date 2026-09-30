module shapes
  implicit none

  type :: circle
    real :: radius
  end type circle

contains

  real function square(x)
    real, intent(in) :: x
    square = x * x
  end function square

  subroutine describe(c)
    type(circle), intent(in) :: c
    call show(square(c%radius))
    call c%render(1)
  end subroutine describe

end module shapes
