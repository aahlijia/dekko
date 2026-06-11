package main

import "fmt"

type Server struct {
	name string
}

func NewServer(name string) *Server {
	return &Server{name: name}
}

func (s *Server) Greet(who string) string {
	return label(who)
}

func label(who string) string {
	return fmt.Sprintf("hi %s", who)
}

func main() {
	s := NewServer("x")
	fmt.Println(s.Greet("y"))
}
