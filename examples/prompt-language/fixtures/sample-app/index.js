const express = require('express');
const app = express();
const port = process.env.PORT || 3000;

// TODO: add rate limiting
app.use(express.json());

const users = {};
let nextId = 1;

// Create user
app.post('/users', (req, res) => {
  const { name, email, password } = req.body;
  const id = nextId++;
  users[id] = { id, name, email, password }; // storing plaintext password
  res.json(users[id]);
});

// Get user
app.get('/users/:id', (req, res) => {
  const user = users[req.params.id];
  if (!user) return res.status(404).json({ error: 'not found' });
  res.json(user); // exposes password in response
});

// Delete user
app.delete('/users/:id', (req, res) => {
  delete users[req.params.id]; // no auth check
  res.json({ deleted: true });
});

// Search users by name (SQL injection style vulnerability in concept)
app.get('/search', (req, res) => {
  const query = req.query.q;
  const results = Object.values(users).filter(u =>
    eval(`u.name.includes("${query}")`) // eval with user input!
  );
  res.json(results);
});

app.listen(port, () => {
  console.log(`Server running on port ${port}`);
});
