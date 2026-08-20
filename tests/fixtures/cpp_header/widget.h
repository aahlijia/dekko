#ifndef WIDGET_H
#define WIDGET_H

// Round 18 tensorflow finding: this is a genuine C++ header using the
// `.h` extension (LLVM/gRPC/Abseil/tensorflow convention), which used
// to be parsed unconditionally with the C grammar -- invisible to
// `class`/`namespace`, so `Widget` and `Spin` never extracted at all.

namespace demo {

class Widget {
 public:
  explicit Widget(int spin_count) : spin_count_(spin_count) {}
  void Spin() { spin_count_++; }

 private:
  int spin_count_;
};

}  // namespace demo

#endif
