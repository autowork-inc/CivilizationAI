import { useState, type Ref } from 'react';
import { Text, TextInput, View, type TextInputProps } from 'react-native';

type Props = TextInputProps & { label: string; ref?: Ref<TextInput> };

export function TextField({ label, ref, onFocus, onBlur, ...inputProps }: Props) {
  const [focused, setFocused] = useState(false);
  return (
    <View className="mb-4">
      <Text className={`mb-2 ml-1 text-sm font-medium ${focused ? 'text-blue-400' : 'text-slate-400'}`}>{label}</Text>
      <TextInput
        ref={ref}
        className={`rounded-2xl border bg-slate-800 px-4 py-4 text-base text-slate-50 ${
          focused ? 'border-blue-500' : 'border-slate-700'
        }`}
        placeholderTextColor="#94a3b8"
        selectionColor="#60a5fa"
        cursorColor="#60a5fa"
        autoCapitalize="none"
        autoCorrect={false}
        onFocus={(e) => {
          setFocused(true);
          onFocus?.(e);
        }}
        onBlur={(e) => {
          setFocused(false);
          onBlur?.(e);
        }}
        {...inputProps}
      />
    </View>
  );
}
