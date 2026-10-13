import { Link } from 'expo-router';
import { useState } from 'react';
import { Pressable, Text, View } from 'react-native';

import { getErrorMessage } from '@/api/apiClient';
import { Button } from '@/components/Button';
import { Screen } from '@/components/Screen';
import { TextField } from '@/components/TextField';
import { useAuth } from '@/context/AuthContext';

const PASSWORD_MIN = 8; // same as the backend

export default function RegisterScreen() {
  const { register } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!email.trim()) {
      setError('Введите почту.');
      return;
    }
    if (password.length < PASSWORD_MIN) {
      setError(`Пароль должен быть не короче ${PASSWORD_MIN} символов.`);
      return;
    }
    setError(null);
    setBusy(true);
    try {
      await register(email, password); // on success the root layout swaps (auth) for (app)
    } catch (e) {
      setError(getErrorMessage(e));
      setBusy(false);
    }
  }

  return (
    <Screen>
      <View className="mb-10">
        <Text className="text-3xl font-bold text-neutral-50">Создать аккаунт</Text>
        <Text className="mt-2 text-base text-neutral-400">Почта и пароль — этого достаточно, чтобы начать.</Text>
      </View>

      <TextField
        label="Email"
        value={email}
        onChangeText={setEmail}
        placeholder="you@example.com"
        keyboardType="email-address"
        textContentType="emailAddress"
        autoComplete="email"
        returnKeyType="next"
      />
      <TextField
        label="Пароль"
        value={password}
        onChangeText={setPassword}
        placeholder={`Не короче ${PASSWORD_MIN} символов`}
        secureTextEntry
        textContentType="newPassword"
        autoComplete="new-password"
        returnKeyType="go"
        onSubmitEditing={submit}
      />

      {error ? <Text className="mb-4 text-sm text-red-400">{error}</Text> : null}

      <Button title="Создать аккаунт" onPress={submit} loading={busy} />

      <Link href="/login" asChild>
        <Pressable className="mt-6 items-center py-2" hitSlop={8}>
          <Text className="text-base text-neutral-400">
            Уже есть аккаунт? <Text className="font-semibold text-indigo-400">Войти</Text>
          </Text>
        </Pressable>
      </Link>
    </Screen>
  );
}
