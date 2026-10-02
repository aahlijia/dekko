package Shape::Circle;
use strict;

sub new {
    my ($class, %args) = @_;
    return bless {%args}, $class;
}

sub area {
    my ($self) = @_;
    return 3.14159 * square($self->{radius});
}

sub square {
    my ($x) = @_;
    return $x * $x;
}

package main;

my $c = Shape::Circle->new(radius => 2);
print $c->area(), "\n";
print &Shape::Circle::square(3), "\n";
