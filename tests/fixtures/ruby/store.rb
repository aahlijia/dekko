def normalize(key)
  key.to_s.strip
end

class Store
  def initialize
    @items = {}
  end

  def put(key, value)
    @items[normalize(key)] = value
  end

  def get(key)
    @items[normalize(key)]
  end
end

store = Store.new
store.put(:a, 1)
