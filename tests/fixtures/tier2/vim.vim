function! shapes#Square(x) abort
  return a:x * a:x
endfunction

function! s:Area(r) abort
  return 3.14159 * shapes#Square(a:r)
endfunction

function! shapes#Describe(r) abort
  echo s:Area(a:r)
endfunction

call shapes#Describe(2)
