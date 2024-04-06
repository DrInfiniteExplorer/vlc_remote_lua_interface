--[==========================================================================[
 remote_lua.lua: Remote lua execution within VLC.
--[==========================================================================[

--]==========================================================================]

local dkjson = require "dkjson"

local port = 9998

local running = true


local function quit_vlc()
    running = false
    vlc.misc.quit()
end

local function log(str)
    vlc.msg.err("Remote:" .. tostring(str))
end

local function execute_limited(func, limit)
    limit = limit or 1000

    local co = coroutine.create(func)
    local result = {timeout=false, result=nil, error=nil}
    debug.sethook(co, function(hook)
            result.timeout = true
            error("Too many instructions in user code!")
        end, '', limit)
    function work()
        local stat, val = coroutine.resume(co)
        if not stat then
            result.error = val
        else
            result.result = val
        end
        return true
    end
    local res, err = pcall(work)
    if not res then
        result.error = err
    end
    return result
end

local function evaluate_code(code, sandbox)
    local chunk, err = loadstring(code)
    if not chunk then
        return {timeout=false, result=nil, error=err}
    end
    return execute_limited(chunk)
end


local function is_array(tbl)
    local cnt = 0
    local indexed = 0
    local max = 0
    for k,v in pairs(tbl) do
        if type(k) == "number" then
            if k <= 0 then return false end
            indexed = indexed + 1
            if k > max then max = k end
        else return false end
        cnt = cnt + 1
    end
    -- treat as array if there are less than 10% holes ? This wildly disregards 
    return indexed > cnt * 0.9, max
end

local function join(sep, arr, seed)
    local res = seed or ""
    for idx, value in ipairs(arr) do
        if idx > 1 then
            res = res .. sep .. value
        else
            res = res .. value
        end
    end
    return res
end

local NULL = {}

local function pp(tbl, max, indent, visited)
    -- Set default indentation level if not provided
    max = max or 4
    indent = indent or 0
    visited = visited or {}

    local tbl_type = type(tbl)
    if tbl_type ~= "table" then
        if tbl_type == "number" or tbl_type == "boolean" then
            return tostring(tbl)
        end
        return dkjson.quotestring(tostring(tbl))
    end

    if tbl == NULL then return "null" end

    if visited[tbl] then
        return "<recursion>"
    end
    visited[tbl] = true

    if indent > 4 then return "<too deep>" end

    local next_indent = indent + 1
    
    local is_arr, max = is_array(tbl)
    
    if is_arr then
        local tmp = {}
        for idx=1,max do
            local val = tbl[idx]
            if val then val = pp(val, max, next_indent, visited) else val = "null" end
            table.insert(tmp, string.rep("    ", next_indent) .. val)
        end
        return "[\n" .. join(",\n", tmp) .. string.rep("    ", indent - 1) .. "]"
    else
        local tmp = {}
        for key, value in pairs(tbl) do
            local line = string.rep("    ", indent) .. dkjson.quotestring(tostring(key)) .. ":" .. pp(value, max, next_indent, visited)
            table.insert(tmp, line)
        end
        return "{\n" .. join(",\n", tmp) .. string.rep("    ", indent - 1) .. "}"
    end
end

--print(pp({1,2,3}))
--print(pp({1,2,3, [5]=4}))
--print(pp({1,2,3, a=4}))
--print(pp({1,2,3, [4]=4}))




local listener = vlc.net.listen_tcp("127.0.0.1", port)
local listener_fd = listener:fds()

local function check_running()
    if vlc.volume.get() == -256 then
        running = false
    end
end

local clients = {}

local function is_set(num, bit_value)
    return (math.floor(num / bit_value) % 2) == 1
end

local function bit_or(a, b)
    local all = true
    for idx=1,32 do
        local bit = 2^idx
        if is_set(b, bit) then
            all = all and is_set(a, bit)
        end
    end
    return all
end

local function bit_any(a, b)
    for idx=1,32 do
        local bit = 2^idx
        if is_set(b, bit) and is_set(a, bit) then return true end
    end
    return false
end

--local fake_listener = vlc.net.listen_tcp("127.0.0.1", port+1)
--local fake_listener_fd = fake_listener:fds()
--vlc.net.close(fake_listener_fd)
local fake_listener_fd = 2

local function nonwait_poll(tbl)
    local tmp = {}

    local cnt=0
    for k,v in pairs(tbl) do tmp[k]=vlc.net.POLLOUT + vlc.net.POLLIN + vlc.net.POLLPRI cnt=cnt+1 end
        
    tmp[fake_listener_fd] = vlc.net.POLLOUT + vlc.net.POLLIN + vlc.net.POLLPRI

    local res = vlc.net.poll(tmp)
    for k,v in pairs(tbl) do tbl[k]=tmp[k] end
    return res-1

end

local function manage_connections(clients)
    local set={}
    local cnt = 0
    for client, fd in pairs(clients) do
        set[fd] = vlc.net.POLLIN
        cnt = cnt + 1
    end
    if cnt == 0 then return {} end

    local to_remove = {}
    local readable = {}
    local poll_result = nonwait_poll(set)

    if poll_result > 0 then
        for client, fd in pairs(clients) do
            local set_entry = set[fd]
            if bit_any(set_entry, vlc.net.POLLERR + vlc.net.POLLHUP + vlc.net.POLLNVAL) then
                log("Client disconnected")
                table.insert(to_remove, client)
            elseif bit_any(set_entry, vlc.net.POLLIN) then
                readable[client] = fd
            end
        end
    end
    for _, client in ipairs(to_remove) do
        clients[client] = nil
        vlc.net.close(client)
    end
    return readable
end

local function sleep(seconds)
    vlc.misc.mwait(vlc.misc.mdate() + seconds * 1000 * 1000) 
end

os.execute("waitfor /SI VlcStarted")

local function run()

    log("HOSTED")

    check_running()

    while running do

        -- accept new connections and select active clients
        local listener_res = nonwait_poll({[listener_fd]=vlc.net.POLLIN})
        if listener_res > 0 then
            local client = listener:accept()
            clients[client] = client
            log("Got a happy client!")
        end

        local to_read = manage_connections(clients)

        for client, fd in pairs(to_read) do
            log("Got stuff to read from " .. tostring(fd))
            local str = vlc.net.recv(client, 10000)
            if not str then break end

            str = string.gsub(str,"\r?\n$","")
            log("GOT " .. str)

            local _, _, msg_len, request_id, code = string.find(str, "^(.-):(.-):(.*)$")
            msg_len, request_id = tonumber(msg_len), tonumber(request_id)
            
            local buffer
            local res = evaluate_code(code, nil)
            if res.error or res.timeout then
                log("Failed to exec")
                res.reply_id = request_id
                res.result = NULL
                buffer = pp(res)
            else
                log("Exec successfull")
                if res.result == nil then res.result = NULL end
                local res = execute_limited(function() return pp({timeout=false, reply_id=request_id, result=res.result}, { indent = true }) end, 10000)
                if res.timeout then
                    buffer = pp({timeout=true, error="Serialization of result took too long", reply_id=request_id})
                else
                    buffer = res.result
                end
            end
            log("> " .. buffer)
            buffer = buffer .. "\n"
            
            vlc.net.send(client, buffer)
        end
        check_running()
        sleep(0.05)
    end

end

while running do
    log("OUTER RUN LOOP")
    local status, res = pcall(run)

    log(pp({status=status, res=res}))
end
