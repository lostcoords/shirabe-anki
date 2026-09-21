# JSON-lines responses from read-only local controller actions.
require "json"
require "digest"

raise "This benchmark requires the development environment" unless [ nil, "development" ].include?(ENV["RAILS_ENV"])
ENV["RAILS_ENV"] = "development"
ENV["OTEL_ENABLE"] = "0"
ENV["OTEL_SDK_DISABLED"] = "true"
ENV["OTEL_TRACES_EXPORTER"] = "none"
protocol = $stdout
$stdout = $stderr
require_relative "../../config/environment"

SemanticLogger.default_level = :warn
Rails.cache = ActiveSupport::Cache::MemoryStore.new(size: 64.megabytes)
[ [ ActiveRecord::Base, "primary" ], [ DictionaryRecord, "dictionary" ] ].each do |model, name|
  config = ActiveRecord::Base.configurations.configs_for(env_name: Rails.env, name: name).configuration_hash
  model.establish_connection(config.merge(readonly: true))
  raise "Database must be read-only" unless model.connection.raw_connection.readonly?
end

user = User.new(name: "Offline corpus fixture", prefs: { "sources" => [ "jmdict:en" ] },
  tokenization_characters_per_request: 5000, tokenization_characters_per_minute: 1_000_000)
credential = ApiKey.new(scopes: [ "user:dictionary:read" ])
routes = {
  [ "POST", "/api/v1/parse" ] => [ Api::V1::ParseController, :create ],
  [ "POST", "/api/v1/words/identify" ] => [ Api::V1::WordsController, :identify ],
  [ "GET", "/api/v1/dictionaries" ] => [ Api::V1::DictionariesController, :index ]
}
call = lambda do |request|
  controller, action = routes.fetch([ request.fetch("method"), request.fetch("path") ])
  body = request["body"]
  env = Rack::MockRequest.env_for(request.fetch("path"), method: request.fetch("method"),
    input: body&.to_json.to_s, "CONTENT_TYPE" => "application/json", "HTTP_ACCEPT_LANGUAGE" => "en")
  env["shirabe.current_user"] = user
  env["shirabe.credential"] = credential
  started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
  status, _headers, response = controller.action(action).call(env)
  output = +""
  response.each { |part| output << part }
  response.close if response.respond_to?(:close)
  elapsed = (Process.clock_gettime(Process::CLOCK_MONOTONIC) - started) * 1000
  raise "Unexpected controller status #{status}" unless status == 200
  raise "Capture response exceeds 4 MiB" if output.bytesize > 4.megabytes

  { response_body: output, controller_ms: elapsed.round(3) }
end
source_response = call.call({ "method" => "GET", "path" => "/api/v1/dictionaries" })
sources = JSON.parse(source_response.fetch(:response_body)).fetch("dictionaries")
  .select { |dictionary| dictionary.fetch("slug") == "jmdict" }
raise "Official JMdict is unavailable" unless sources.one? && sources.first["visibility"] == "official"

source_hash = Digest::SHA256.new
Dir["app/{controllers,models}/**/*.rb"].sort.push("Gemfile.lock").each do |path|
  source_hash << path << Digest::SHA256.file(path).hexdigest
end
protocol.puts({ ready: true, readonly: true, capture_version: 2, telemetry: false, server_source_sha256: source_hash.hexdigest,
  scope: "Read-only real controller actions; unsaved fixture identity, no HTTP middleware or WAN",
  sources: sources, preferences: { stackFingerprint: user.stack_fingerprint,
    dictionaries: [ "jmdict:en" ], languages: [ "en" ], uiLocale: "en", dictionaryReveal: {} } }.to_json)
protocol.flush

while (line = $stdin.gets(65_537))
  raise "Capture request exceeds 64 KiB" if line.bytesize > 65_536 || !line.end_with?("\n")

  protocol.puts(call.call(JSON.parse(line)).to_json)
  protocol.flush
  ActiveSupport::ExecutionContext.clear
end
