#include "Json.hpp"

#include <cstdio>
#include <cstdlib>

namespace smbwap::util::json {

Encoder& Encoder::beginObject() { maybeComma(); out_.append('{'); pushFrame(); return *this; }
Encoder& Encoder::endObject() {
    out_.append('}');
    popFrame();
    markNeedsComma();
    return *this;
}
Encoder& Encoder::beginArray()  { maybeComma(); out_.append('['); pushFrame(); return *this; }
Encoder& Encoder::endArray() {
    out_.append(']');
    popFrame();
    markNeedsComma();
    return *this;
}

Encoder& Encoder::key(std::string_view k) {
    maybeComma();
    out_.append('"'); out_.append(k.data(), k.size()); out_.append('"'); out_.append(':');
    clearNeedsComma();
    return *this;
}

Encoder& Encoder::value(std::string_view s) {
    maybeComma();
    out_.append('"');
    for (char c : s) {
        switch (c) {
            case '"':  out_.append('\\'); out_.append('"');  break;
            case '\\': out_.append('\\'); out_.append('\\'); break;
            case '\n': out_.append('\\'); out_.append('n');  break;
            case '\r': out_.append('\\'); out_.append('r');  break;
            case '\t': out_.append('\\'); out_.append('t');  break;
            default:   out_.append(c);                       break;
        }
    }
    out_.append('"');
    markNeedsComma();
    return *this;
}

Encoder& Encoder::value(std::int64_t v) {
    maybeComma();
    char tmp[24];
    int n = std::snprintf(tmp, sizeof(tmp), "%lld", static_cast<long long>(v));
    if (n > 0) out_.append(tmp, static_cast<std::size_t>(n));
    markNeedsComma();
    return *this;
}
Encoder& Encoder::value(int v) { return value(static_cast<std::int64_t>(v)); }

Encoder& Encoder::value(bool v) {
    maybeComma();
    if (v) out_.append("true", 4);
    else   out_.append("false", 5);
    markNeedsComma();
    return *this;
}

void Encoder::maybeComma() {
    if (depth_ == 0) return;
    if (needs_comma_stack_[depth_ - 1]) out_.append(',');
}

void Encoder::pushFrame() {
    if (depth_ < kMaxDepth) {
        needs_comma_stack_[depth_] = false;
        ++depth_;
    }
}

void Encoder::popFrame() {
    if (depth_ > 0) --depth_;
}

void Encoder::markNeedsComma() {
    if (depth_ > 0) needs_comma_stack_[depth_ - 1] = true;
}

void Encoder::clearNeedsComma() {
    if (depth_ > 0) needs_comma_stack_[depth_ - 1] = false;
}

Reader::Reader(const char* data, std::size_t len) : p_(data), end_(data + len) {}

void Reader::skipWs() {
    while (p_ < end_ && (*p_ == ' ' || *p_ == '\t' || *p_ == '\n' || *p_ == '\r')) ++p_;
}

bool Reader::fail() {
    error_ = true;
    return false;
}

bool Reader::prepareValue() {
    if (error_) return false;
    if (depth_ > 0 && !stack_[depth_ - 1].is_object && stack_[depth_ - 1].needs_comma) {
        skipWs();
        if (p_ >= end_ || *p_ != ',') return fail();
        ++p_;
    }
    skipWs();
    return p_ < end_;
}

void Reader::markValueDone() {
    if (depth_ > 0 && !stack_[depth_ - 1].is_object) {
        stack_[depth_ - 1].needs_comma = true;
    }
}

namespace {
inline bool isHexDigit(char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}
inline int hexVal(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return c - 'A' + 10;
}
}  // namespace

bool Reader::readString(std::string_view& out) {
    if (p_ >= end_ || *p_ != '"') return false;
    ++p_;
    char* write = const_cast<char*>(p_);
    const char* start = write;
    while (p_ < end_ && *p_ != '"') {
        char c = *p_;
        if (c == '\\') {
            ++p_;
            if (p_ >= end_) return false;
            char esc = *p_++;
            switch (esc) {
                case '"':  *write++ = '"';  break;
                case '\\': *write++ = '\\'; break;
                case '/':  *write++ = '/';  break;
                case 'n':  *write++ = '\n'; break;
                case 'r':  *write++ = '\r'; break;
                case 't':  *write++ = '\t'; break;
                case 'b':  *write++ = '\b'; break;
                case 'f':  *write++ = '\f'; break;
                case 'u': {
                    if (end_ - p_ < 4) return false;
                    unsigned cp = 0;
                    for (int i = 0; i < 4; ++i) {
                        char h = *p_++;
                        if (!isHexDigit(h)) return false;
                        cp = (cp << 4) | static_cast<unsigned>(hexVal(h));
                    }
                    if (cp < 0x80u) {
                        *write++ = static_cast<char>(cp);
                    } else if (cp < 0x800u) {
                        *write++ = static_cast<char>(0xC0u | (cp >> 6));
                        *write++ = static_cast<char>(0x80u | (cp & 0x3Fu));
                    } else {
                        *write++ = static_cast<char>(0xE0u | (cp >> 12));
                        *write++ = static_cast<char>(0x80u | ((cp >> 6) & 0x3Fu));
                        *write++ = static_cast<char>(0x80u | (cp & 0x3Fu));
                    }
                    break;
                }
                default: return false;
            }
        } else {
            *write++ = c;
            ++p_;
        }
    }
    if (p_ >= end_ || *p_ != '"') return false;
    ++p_;
    out = std::string_view(start, static_cast<std::size_t>(write - start));
    return true;
}

bool Reader::enterObject() {
    if (!prepareValue()) return fail();
    if (*p_ != '{') return fail();
    ++p_;
    if (depth_ >= kMaxDepth) return fail();
    stack_[depth_++] = Frame{true, false};
    return true;
}

bool Reader::exitObject() {
    if (error_) return false;
    if (depth_ == 0 || !stack_[depth_ - 1].is_object) return fail();
    skipWs();
    if (p_ >= end_ || *p_ != '}') return fail();
    ++p_;
    --depth_;
    markValueDone();
    return true;
}

bool Reader::nextField(std::string_view& out_key) {
    if (error_) return false;
    if (depth_ == 0 || !stack_[depth_ - 1].is_object) return fail();
    skipWs();
    if (p_ >= end_) return fail();
    if (*p_ == '}') return false;
    Frame& f = stack_[depth_ - 1];
    if (f.needs_comma) {
        if (*p_ != ',') return fail();
        ++p_;
        skipWs();
    }
    if (p_ >= end_ || *p_ != '"') return fail();
    if (!readString(out_key)) return fail();
    skipWs();
    if (p_ >= end_ || *p_ != ':') return fail();
    ++p_;
    skipWs();
    f.needs_comma = true;
    return true;
}

bool Reader::enterArray() {
    if (!prepareValue()) return fail();
    if (*p_ != '[') return fail();
    ++p_;
    if (depth_ >= kMaxDepth) return fail();
    stack_[depth_++] = Frame{false, false};
    return true;
}

bool Reader::exitArray() {
    if (error_) return false;
    if (depth_ == 0 || stack_[depth_ - 1].is_object) return fail();
    skipWs();
    if (p_ >= end_ || *p_ != ']') return fail();
    ++p_;
    --depth_;
    markValueDone();
    return true;
}

bool Reader::hasMoreInArray() const {
    if (error_) return false;
    if (depth_ == 0 || stack_[depth_ - 1].is_object) return false;
    const char* q = p_;
    while (q < end_ && (*q == ' ' || *q == '\t' || *q == '\n' || *q == '\r')) ++q;
    if (q >= end_) return false;
    return *q != ']';
}

bool Reader::nextString(std::string_view& out) {
    if (!prepareValue()) return fail();
    if (!readString(out)) return fail();
    markValueDone();
    return true;
}

bool Reader::nextInt(std::int64_t& out) {
    if (!prepareValue()) return fail();
    bool neg = false;
    if (*p_ == '-') {
        neg = true;
        ++p_;
        if (p_ >= end_) return fail();
    }
    if (*p_ < '0' || *p_ > '9') return fail();
    std::int64_t v = 0;
    while (p_ < end_ && *p_ >= '0' && *p_ <= '9') {
        v = v * 10 + (*p_ - '0');
        ++p_;
    }
    if (p_ < end_ && (*p_ == '.' || *p_ == 'e' || *p_ == 'E')) return fail();
    out = neg ? -v : v;
    markValueDone();
    return true;
}

bool Reader::nextBool(bool& out) {
    if (!prepareValue()) return fail();
    if (end_ - p_ >= 4 && p_[0] == 't' && p_[1] == 'r' && p_[2] == 'u' && p_[3] == 'e') {
        p_ += 4;
        out = true;
        markValueDone();
        return true;
    }
    if (end_ - p_ >= 5 && p_[0] == 'f' && p_[1] == 'a' && p_[2] == 'l' && p_[3] == 's' && p_[4] == 'e') {
        p_ += 5;
        out = false;
        markValueDone();
        return true;
    }
    return fail();
}

bool Reader::isNull() {
    if (error_) return false;
    const char* save = p_;
    if (depth_ > 0 && !stack_[depth_ - 1].is_object && stack_[depth_ - 1].needs_comma) {
        skipWs();
        if (p_ >= end_ || *p_ != ',') { p_ = save; return false; }
        ++p_;
    }
    skipWs();
    if (end_ - p_ >= 4 && p_[0] == 'n' && p_[1] == 'u' && p_[2] == 'l' && p_[3] == 'l') {
        p_ += 4;
        markValueDone();
        return true;
    }
    p_ = save;
    return false;
}

bool Reader::skipString() {
    // Step over a string literal without decoding it (no in-place write).
    // A backslash skips the next byte; for \uXXXX the hex digits are then
    // ordinary bytes, so the loop naturally runs to the closing quote.
    if (p_ >= end_ || *p_ != '"') return false;
    ++p_;
    while (p_ < end_ && *p_ != '"') {
        if (*p_ == '\\') {
            ++p_;
            if (p_ >= end_) return false;
        }
        ++p_;
    }
    if (p_ >= end_) return false;
    ++p_;
    return true;
}

bool Reader::matchLiteral(const char* lit, std::size_t n) {
    if (static_cast<std::size_t>(end_ - p_) < n) return false;
    for (std::size_t i = 0; i < n; ++i) {
        if (p_[i] != lit[i]) return false;
    }
    p_ += n;
    return true;
}

// Consume exactly one JSON value of any type at the cursor.
//
// This is what every parseXxx() "unknown field" branch in ApProtocol.cpp
// uses to stay in sync with a newer bridge that has added a field this
// build does not know about.  The previous idiom chained
// isNull()/nextString()/nextInt()/nextBool(), but nextString() marks the
// sticky error_ flag as soon as the first byte is not '"', so every later
// call short-circuited and an unknown int/bool field rejected the whole
// line ("[conn] decode failed").  Forward compatibility is the point of
// the branch, so this primitive never sets error_ on a type mismatch --
// only on genuinely malformed input (truncated, unbalanced, bad byte).
//
// Nested objects/arrays are skipped by counting brackets over the raw
// text (strings are stepped over so brackets inside them don't count),
// capped at kMaxSkipDepth.  No allocation and no frame-stack use: the
// skipped container is never "entered", so depth_ is untouched.
bool Reader::skipValue() {
    if (!prepareValue()) return fail();
    const char c = *p_;
    if (c == '"') {
        if (!skipString()) return fail();
    } else if (c == '{' || c == '[') {
        int nest = 0;
        do {
            if (p_ >= end_) return fail();
            const char d = *p_;
            if (d == '"') {
                if (!skipString()) return fail();
                continue;
            }
            if (d == '{' || d == '[') {
                if (++nest > kMaxSkipDepth) return fail();
            } else if (d == '}' || d == ']') {
                --nest;
            }
            ++p_;
        } while (nest > 0);
    } else if (c == '-' || (c >= '0' && c <= '9')) {
        // Number: accept the whole JSON number charset (fraction and
        // exponent included).  We never interpret it, only step over it.
        ++p_;
        while (p_ < end_ && ((*p_ >= '0' && *p_ <= '9') || *p_ == '.' ||
                             *p_ == 'e' || *p_ == 'E' || *p_ == '+' || *p_ == '-')) {
            ++p_;
        }
    } else if (matchLiteral("true", 4) || matchLiteral("false", 5) ||
               matchLiteral("null", 4)) {
        // Literal consumed.
    } else {
        return fail();
    }
    markValueDone();
    return true;
}

}  // namespace smbwap::util::json
