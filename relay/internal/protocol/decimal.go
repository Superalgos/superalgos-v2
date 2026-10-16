package protocol

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math/big"
	"regexp"
	"strings"
)

var decRe = regexp.MustCompile(`^[0-9]+(\.[0-9]+)?$`)

// Num is a JSON value that may be written as a number or a string ("0.1" or 0.1). The raw text
// is kept so that signing messages can be built from a canonical decimal form.
type Num string

// UnmarshalJSON accepts a JSON number or string.
func (n *Num) UnmarshalJSON(b []byte) error {
	b = bytes.TrimSpace(b)
	if len(b) > 0 && b[0] == '"' {
		var s string
		if err := json.Unmarshal(b, &s); err != nil {
			return err
		}
		*n = Num(s)
		return nil
	}
	if bytes.Equal(b, []byte("null")) {
		*n = ""
		return nil
	}
	*n = Num(string(b))
	return nil
}

// Canonical validates a plain non-negative decimal (no sign, no exponent) and returns its canonical
// text: no leading zeros (except a single 0 before the point), no trailing fractional zeros, no
// trailing point. Both signer and relay build messages from this form, so 0.10, 0.1 and "0.1" agree.
func Canonical(raw string, maxInt, maxFrac int) (string, error) {
	if !decRe.MatchString(raw) {
		return "", fmt.Errorf("%q is not a plain decimal number (digits with an optional fraction; no sign or exponent)", raw)
	}
	i, f, _ := strings.Cut(raw, ".")
	i = strings.TrimLeft(i, "0")
	if i == "" {
		i = "0"
	}
	f = strings.TrimRight(f, "0")
	if len(i) > maxInt {
		return "", fmt.Errorf("%q has more than %d integer digits", raw, maxInt)
	}
	if len(f) > maxFrac {
		return "", fmt.Errorf("%q has more than %d fractional digits", raw, maxFrac)
	}
	if f == "" {
		return i, nil
	}
	return i + "." + f, nil
}

// Rat converts canonical decimal text to an exact rational.
func Rat(canonical string) *big.Rat {
	r, _ := new(big.Rat).SetString(canonical)
	return r
}

var weiPerToken = new(big.Int).Exp(big.NewInt(10), big.NewInt(18), nil)

// WeiPerToken is 10^18, the unit all power comparisons use.
func WeiPerToken() *big.Int { return new(big.Int).Set(weiPerToken) }

// TokensToWei converts canonical decimal tokens to 18-decimal base units, rounding down.
func TokensToWei(canonical string) *big.Int {
	r := Rat(canonical)
	r.Mul(r, new(big.Rat).SetInt(weiPerToken))
	return new(big.Int).Quo(r.Num(), r.Denom())
}
