// A tiny module with one obvious bug: `add` subtracts instead of adding.
function add(a, b) {
  return a - b; // BUG: should be a + b
}

function multiply(a, b) {
  return a * b;
}

module.exports = { add, multiply };
