#!/usr/bin/env ruby
# Bounded JSON-lines adapter around pinned Wayback Machine Downloader.
# The upstream MIT licence is preserved under vendor/wayback-machine-downloader.

require 'net/http'
require 'json'
require 'digest'
require 'time'
require 'stringio'
require 'zlib'
require_relative 'vendor/wayback-machine-downloader/lib/wayback_machine_downloader'

class CaptureFailure < StandardError; end

class PersistentWayback
  attr_reader :requests, :bytes, :connections

  def initialize(config)
    @origin = URI(config.fetch('origin', 'https://web.archive.org'))
    unless @origin.to_s == 'https://web.archive.org' ||
           (@origin.scheme == 'http' && ['127.0.0.1', 'localhost'].include?(@origin.host))
      raise CaptureFailure, 'Only Wayback or a loopback test fixture is allowed'
    end
    @delay = Float(config.fetch('delay', 3))
    @rate = Integer(config.fetch('bytes_per_second', 131072))
    @maximum = Integer(config.fetch('max_requests', 240))
    @body_limit = Integer(config.fetch('max_response_bytes', 1048576))
    @byte_limit = Integer(config.fetch('max_total_bytes', 25165824))
    @started = clock
    duration = Float(config.fetch('max_seconds', 900))
    @deadline = @started + duration
    if !@delay.finite? || @delay < 0 || @rate <= 0 || @maximum <= 0 || @body_limit <= 0 || @byte_limit <= 0 || !duration.finite? || duration <= 0
      raise CaptureFailure, 'Invalid transport bounds'
    end
    @requests = @bytes = @connections = 0
    @last = nil
  end

  def clock
    Process.clock_gettime(Process::CLOCK_MONOTONIC)
  end

  def close
    @http.finish if @http && @http.started?
    @http = nil
  rescue IOError
    @http = nil
  end

  def connection
    return @http if @http && @http.started?
    @http = Net::HTTP.new(@origin.host, @origin.port, nil)
    @http.use_ssl = @origin.scheme == 'https'
    @http.open_timeout = [12, @deadline - clock].min
    @http.read_timeout = [20, @deadline - clock].min
    @http.keep_alive_timeout = 60
    @http.max_retries = 0
    @http.start
    @connections += 1
    @http
  end

  def pause(seconds)
    raise CaptureFailure, 'Wayback wall-clock budget reached' if clock + seconds >= @deadline
    sleep(seconds) if seconds > 0
  end

  def get(path, redirects = 0)
    raise CaptureFailure, 'Too many replay redirects' if redirects > 4
    attempts = 0
    loop do
      raise CaptureFailure, 'Wayback request budget reached' if @requests >= @maximum
      pause(@last ? [@delay - (clock - @last), 0].max : 0)
      @last = clock
      @requests += 1
      attempts += 1
      body = ''.b
      response = nil
      started = clock
      begin
        request = Net::HTTP::Get.new(path)
        request['User-Agent'] = 'EQArchivesCandidateCrawler/1.0 (+https://search.eqarchives.org)'
        request['Accept-Encoding'] = 'identity'
        http = connection
        http.read_timeout = [20, @deadline - clock].min
        http.request(request) do |reply|
          response = reply
          reply.read_body do |chunk|
            @bytes += chunk.bytesize
            raise CaptureFailure, 'Wayback total byte budget reached' if @bytes > @byte_limit
            raise CaptureFailure, 'Response exceeds byte limit; capture not saved' if body.bytesize + chunk.bytesize > @body_limit
            body << chunk
            pause([body.bytesize.to_f / @rate - (clock - started), 0].max)
          end
        end
      rescue CaptureFailure
        close
        raise
      rescue EOFError, IOError, SystemCallError, Net::OpenTimeout, Net::ReadTimeout, OpenSSL::SSL::SSLError
        close
        raise CaptureFailure, 'Wayback connection failed after bounded retries' if attempts >= 2
        pause(5)
        next
      end
      status = response.code.to_i
      if [422, 429, 500, 502, 503, 504].include?(status)
        raise CaptureFailure, "Wayback HTTP #{status} after bounded retries" if attempts >= 3
        # Keep the established connection; back off every request, including CDX.
        retry_after = response['retry-after'].to_s
        wait = retry_after.match?(/^\d+$/) ? [[retry_after.to_i, 5].max, 45].min : 5 * (2 ** (attempts - 1))
        pause(wait)
        next
      end
      if [301, 302, 303, 307, 308].include?(status)
        destination = URI.join(@origin.to_s + path, response['location'].to_s)
        unless destination.host == @origin.host && destination.port == @origin.port && destination.scheme == @origin.scheme
          raise CaptureFailure, 'Replay attempted to redirect outside Wayback'
        end
        return get(destination.request_uri, redirects + 1)
      end
      raise CaptureFailure, "Wayback HTTP #{status}" unless status == 200
      if response['content-encoding'].to_s.downcase == 'gzip'
        body = Zlib::GzipReader.wrap(StringIO.new(body)) { |reader| reader.read(@body_limit + 1) }
        raise CaptureFailure, 'Expanded response exceeds byte limit' if body.bytesize > @body_limit
      end
      return { 'body' => body, 'path' => path, 'content_type' => response['content-type'],
               'memento_datetime' => response['memento-datetime'] }
    end
  end

  def stats
    { 'requests' => @requests, 'bytes' => @bytes, 'connections' => @connections, 'seconds' => clock - @started }
  end
end

class BoundedDownloader < WaybackMachineDownloader
  attr_reader :limited, :available_rows, :identity_variants

  def initialize(job, transport)
    @job, @transport = job, transport
    @limited = false
    @available_rows = 0
    @identity_variants = []
    super(base_url: job.fetch('url'), exact_url: true, all_timestamps: true,
          from_timestamp: job.fetch('from'), to_timestamp: job.fetch('to'), threads_count: 1)
  end

  def get_all_snapshots_to_consider
    get_raw_list_from_api(@base_url, nil)
  end

  def get_raw_list_from_api(url, _page)
    params = [['url', url], ['matchType', 'exact'], ['output', 'json'],
              ['fl', 'timestamp,original,mimetype,statuscode,digest,length'],
              ['filter', 'statuscode:200'], ['filter', 'mimetype:text/html'],
              ['from', @from_timestamp.to_s], ['to', @to_timestamp.to_s],
              ['collapse', 'digest'], ['limit', '80'], ['showResumeKey', 'true']]
    result = @transport.get('/cdx/search/cdx?' + URI.encode_www_form(params))
    rows = JSON.parse(result.fetch('body'))
    return [] if rows == []
    raise CaptureFailure, 'Unexpected CDX schema' unless rows.is_a?(Array)
    header = rows.shift
    raise CaptureFailure, 'Unexpected CDX schema' unless header == ['timestamp', 'original', 'mimetype', 'statuscode', 'digest', 'length']
    @catalog = rows.select { |row| row.is_a?(Array) && row.length == 6 }
    unless rows.all? { |row| row.is_a?(Array) && [0, 1, 6].include?(row.length) }
      raise CaptureFailure, 'Unexpected CDX record'
    end
    unless @catalog.all? { |row| row.all? { |field| field.is_a?(String) } && row[2] == 'text/html' && row[3] == '200' }
      raise CaptureFailure, 'Unexpected CDX record'
    end
    @limited = @catalog.length >= 80 || rows.any? { |row| row.is_a?(Array) && row.length == 1 }
    @catalog.map { |row| [row[0], row[1]] }
  rescue JSON::ParserError
    raise CaptureFailure, 'CDX returned invalid JSON; availability remains unresolved'
  end

  def get_file_list_all_timestamps
    # Avoid upstream's CGI::unescape identity collisions between URLs, schemes
    # and encoded query strings. Filenames are derived from the exact URL.
    get_all_snapshots_to_consider.each_with_object({}) do |(timestamp, url), files|
      next unless same_original?(url, @base_url) && timestamp.match?(/^\d{14}$/)
      next unless timestamp >= @from_timestamp.to_s && timestamp <= @to_timestamp.to_s
      identifier = timestamp + '/' + Digest::SHA256.hexdigest(url) + '.html'
      files[identifier] = { file_url: url, timestamp: timestamp }
    end
  end

  def same_original?(left, right)
    # Historic CDX records frequently spell out :80 or :443. Preserve their
    # original spelling in manifests while comparing only default-port aliases.
    a, b = URI(left), URI(right)
    !a.userinfo && !b.userinfo && a.scheme == b.scheme && a.host&.downcase == b.host&.downcase && a.port == b.port &&
      (a.path.empty? ? '/' : a.path) == (b.path.empty? ? '/' : b.path) && a.query == b.query
  rescue URI::InvalidURIError
    false
  end

  def catalog
    # Use upstream's public timestamp-preserving file-list interface.
    @catalog = []
    files = get_file_list_by_timestamp.map do |item|
      row = @catalog.find { |r| r[0] == item[:timestamp] && r[1] == item[:file_url] }
      { 'url' => item[:file_url], 'timestamp' => item[:timestamp], 'digest' => row[4], 'length' => row[5] }
    end
    @available_rows = @catalog.length
    @identity_variants = @catalog.map { |row| row[1] }.uniq.reject { |url| same_original?(url, @base_url) }.first(8)
    files
  end

  # Ezboard boards/forums are sibling URL prefixes, not directories. Keep
  # this operation narrow: no host/domain-wide CDX scans, arbitrary wildcards,
  # digest collapse (which can hide distinct pages), or parallel connections.
  def ezboard_catalog
    uri = URI(@base_url)
    unless ['http', 'https'].include?(uri.scheme) && !uri.userinfo &&
           uri.host&.match?(/\A(?:www\.)?(?:server|pub|p|b)[0-9]+\.ezboard\.com\z/i) &&
           uri.port == (uri.scheme == 'https' ? 443 : 80) && !uri.query && !uri.fragment &&
           uri.path.match?(/\A\/[bf][a-zA-Z0-9_]{1,120}\z/)
      raise CaptureFailure, 'Ezboard listing requires a board or forum prefix on a numbered Ezboard server'
    end
    paginated_catalog('prefix')
  rescue URI::InvalidURIError
    raise CaptureFailure, 'Invalid Ezboard listing URL'
  end

  def capture_catalog
    # A complete dated listing for one approved original page. CDX can return
    # scheme/query variants even for exact matching; retain exact identity.
    result = paginated_catalog('exact')
    result['captures'].select! { |row| same_original?(row['url'], @base_url) }
    result
  end

  def sitepowerup_catalog
    uri = URI(@base_url)
    fields = URI.decode_www_form(uri.query || '')
    prefix = fields.length == 1 || (fields.length == 2 && fields[0][0] == 'Action' && ['Display', 'Reply'].include?(fields[0][1]))
    unless ['http', 'https'].include?(uri.scheme) && !uri.userinfo && !uri.fragment &&
           ['sitepowerup.com', 'www.sitepowerup.com'].include?(uri.host&.downcase) &&
           uri.port == (uri.scheme == 'https' ? 443 : 80) && uri.path == '/mb/view.asp' && prefix &&
           fields.last[0] == 'BoardID' && fields.last[1].match?(/\A[1-9][0-9]{0,17}\z/)
      raise CaptureFailure, 'SitePowerUp listing requires an explicit numeric BoardID and a read-only view prefix'
    end
    # CDX canonicalizes query order. Preserve every original spelling/date;
    # the capture engine checks BoardID exactly (102010 is not 1020100).
    paginated_catalog('prefix')
  rescue URI::InvalidURIError, ArgumentError
    raise CaptureFailure, 'Invalid SitePowerUp listing URL'
  end

  def paginated_catalog(match)
    params = [['url', @base_url], ['matchType', match], ['output', 'json'],
              ['fl', 'timestamp,original,mimetype,statuscode,digest,length'],
              ['filter', 'statuscode:200'], ['filter', 'mimetype:text/html'],
              ['from', @from_timestamp.to_s], ['to', @to_timestamp.to_s],
              ['limit', '200'], ['showResumeKey', 'true']]
    if @job['resume_key']
      key = @job['resume_key']
      unless key.is_a?(String) && key.bytesize.between?(1, 4096) && key.match?(/\A[\x21-\x7e]+\z/)
        raise CaptureFailure, 'Invalid CDX resume key'
      end
      # CDX emits an already URL-encoded resumption key. Decode once before
      # encode_www_form; double encoding resumes at the wrong position.
      params << ['resumeKey', URI.decode_www_form_component(key)]
    end
    rows = JSON.parse(@transport.get('/cdx/search/cdx?' + URI.encode_www_form(params)).fetch('body'))
    return { 'captures' => [], 'resume_key' => nil } if rows == []
    unless rows.is_a?(Array) && rows.shift == ['timestamp', 'original', 'mimetype', 'statuscode', 'digest', 'length']
      raise CaptureFailure, 'Unexpected CDX schema'
    end
    resume = nil
    if rows.last.is_a?(Array) && rows.last.length == 1
      resume = rows.pop[0]
      unless resume.is_a?(String) && resume.bytesize.between?(1, 4096) && resume.match?(/\A[\x21-\x7e]+\z/)
        raise CaptureFailure, 'Invalid CDX resume key'
      end
      raise CaptureFailure, 'Unexpected CDX continuation' unless rows.pop == []
    end
    unless rows.length <= 200 && rows.all? { |row| row.is_a?(Array) && row.length == 6 &&
      row.all? { |field| field.is_a?(String) } && row[0].match?(/\A\d{14}\z/) &&
      row[0] >= @from_timestamp.to_s && row[0] <= @to_timestamp.to_s && row[2] == 'text/html' && row[3] == '200' }
      raise CaptureFailure, 'Unexpected CDX record'
    end
    { 'captures' => rows.map { |r| { 'timestamp' => r[0], 'url' => r[1], 'digest' => r[4], 'length' => r[5] } },
      'resume_key' => resume }
  rescue JSON::ParserError
    raise CaptureFailure, 'CDX returned invalid JSON; availability remains unresolved'
  end

  def capture
    timestamp = @job.fetch('timestamp')
    raise CaptureFailure, 'Capture timestamp invalid' unless timestamp.match?(/^\d{14}$/)
    download_file(file_url: @base_url, timestamp: timestamp)
  end

  def download_file(info)
    replay = '/web/' + info.fetch(:timestamp) + 'id_/' + info.fetch(:file_url)
    response = @transport.get(replay)
    actual = response['path'][%r{^/web/(\d{14})}, 1]
    basis = 'replay_url'
    if response['memento_datetime']
      actual = Time.httpdate(response['memento_datetime']).utc.strftime('%Y%m%d%H%M%S')
      basis = 'memento_datetime'
    end
    raise CaptureFailure, 'Returned capture timestamp unavailable' unless actual
    unless actual >= @from_timestamp.to_s && actual <= @to_timestamp.to_s
      raise CaptureFailure, 'Wayback returned a capture outside the requested tier'
    end
    match = response['path'].match(%r{^/web/\d{14}(?:[a-z]+_)?/(https?://.+)$})
    raise CaptureFailure, 'Replay returned a different original URL' unless match && match[1] == @base_url
    body = response.fetch('body')
    raise CaptureFailure, 'Empty source body' if body.empty?
    destination = @job.fetch('destination')
    File.open(destination + '.part', 'wb', 0600) { |file| file.write(body) }
    File.rename(destination + '.part', destination)
    { 'url' => @base_url, 'requested_timestamp' => info.fetch(:timestamp), 'timestamp' => actual,
      'timestamp_basis' => basis, 'content_type' => response['content_type'],
      'sha256' => Digest::SHA256.hexdigest(body), 'bytes' => body.bytesize }
  end
end

if $PROGRAM_NAME == __FILE__
  transport = nil
  STDOUT.sync = true
  STDIN.each_line do |line|
    begin
      job = JSON.parse(line)
      if job.fetch('op') == 'configure'
        transport&.close
        transport = PersistentWayback.new(job)
        result = { 'configured' => true }
      else
        raise CaptureFailure, 'Configure transport first' unless transport
        downloader = BoundedDownloader.new(job, transport)
        result = case job['op']
                 when 'list' then { 'captures' => downloader.catalog, 'listing_limited' => downloader.limited,
                                    'available_rows' => downloader.available_rows, 'identity_variants' => downloader.identity_variants }
                 when 'capture' then downloader.capture
                 when 'ezboard_list' then downloader.ezboard_catalog
                 when 'sitepowerup_list' then downloader.sitepowerup_catalog
                 when 'capture_list' then downloader.capture_catalog
                 else raise CaptureFailure, 'Unknown downloader operation'
                 end
      end
      puts JSON.generate({ 'ok' => true, 'result' => result, 'transport' => transport.stats })
    rescue CaptureFailure, JSON::ParserError, KeyError, ArgumentError, Zlib::Error => error
      puts JSON.generate({ 'ok' => false, 'error' => error.message, 'transport' => transport&.stats })
    end
  end
  transport&.close
end
