# Run inside a GitLab Rails container. Emit only the configured database roles.
require 'openssl'
require 'socket'
require 'timeout'
require 'uri'
require 'yaml'

def verify_peer(host, port, ca_file, postgres: false)
  raise 'missing CA file' unless ca_file.is_a?(String) && File.file?(ca_file) && File.readable?(ca_file)

  Timeout.timeout(10) do
    Socket.tcp(host, port, connect_timeout: 5) do |socket|
      if postgres
        # Request TLS before sending any PostgreSQL startup message.
        socket.write([8, 80877103].pack('NN'))
        raise 'server declined TLS' unless socket.read(1) == 'S'
      end
      context = OpenSSL::SSL::SSLContext.new
      context.ca_file = ca_file
      context.verify_mode = OpenSSL::SSL::VERIFY_PEER
      context.verify_hostname = true
      tls = OpenSSL::SSL::SSLSocket.new(socket, context)
      tls.hostname = host
      tls.connect
      tls.close
    end
  end
end

stage = 'postgres-config'
begin
  postgres_host, cache_host = ARGV
  config = ENV.fetch('CONFIG_DIRECTORY', '/srv/gitlab/config')
  databases = YAML.safe_load_file(File.join(config, 'database.yml'), aliases: true).fetch('production')
  raise 'missing database configuration' if databases.empty? || !ENV.fetch('DATABASE_URL', '').empty?

  roles = databases.values.map do |db|
    stage = 'postgres-config'
    role = db.fetch('username')
    unless db.fetch('adapter') == 'postgresql' && db.fetch('host') == postgres_host &&
           Integer(db.fetch('port')) == 5432 && db['url'].to_s.empty? &&
           role.is_a?(String) && role.match?(/\A[a-z0-9_][a-z0-9_-]{0,62}\z/) &&
           db.fetch('sslmode', ENV['PGSSLMODE']) == 'verify-full'
      raise 'unverified database configuration'
    end
    stage = 'postgres-peer'
    verify_peer(postgres_host, 5432, db.fetch('sslrootcert', ENV['PGSSLROOTCERT']), postgres: true)
    role
  end

  stage = 'cache-config'
  configs = [File.join(config, 'resque.yml'), *Dir.glob(File.join(config, 'redis.*.yml'))]
  configs << File.join(config, 'cable.yml') if File.file?(File.join(config, 'cable.yml'))
  # The pinned profile uses Rails' per-role Redis configs, with no redis.yml override.
  raise 'unexpected Redis override' if File.size?(File.join(config, 'redis.yml'))
  cache_roots = configs.map do |path|
    cache = YAML.safe_load_file(path, aliases: true).fetch('production')
    uri = URI.parse(cache.fetch('url'))
    unless uri.scheme == 'rediss' && uri.host == cache_host && uri.port == 6379 && cache['ssl'] == true
      raise 'unverified cache configuration'
    end
    cache.fetch('ssl_params').fetch('ca_file')
  end
  stage = 'cache-peer'
  cache_roots.uniq.each { |root| verify_peer(cache_host, 6379, root) }
  puts roles.uniq
rescue StandardError
  warn "gitlab_data_tls=failed stage=#{stage}"
  exit 1
end
