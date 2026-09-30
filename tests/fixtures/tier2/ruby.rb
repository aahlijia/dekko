# A store with a helper.
def normalize(key)
  key.to_s.strip
end

module Storage
  class Store
    def initialize
      @items = {}
    end

    def put(key, value)
      @items[normalize(key)] = value
    end

    def self.build
      new
    end
  end
end

Storage::Store.build.put("a", 1)
