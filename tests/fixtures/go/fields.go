package p

type S struct {
	a Foo
	b *Bar
	c, d int
	Embedded
	in struct{ z int }
}

func (s *S) Run(x int) {}
