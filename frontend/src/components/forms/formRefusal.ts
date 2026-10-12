/**
 * Thrown by an `onSubmit` that decides, before or instead of calling the API,
 * that the submit must not go ahead — the record changed underneath the form,
 * say. Its message is shown to the user exactly as an API refusal would be.
 */
export class FormRefusal extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'FormRefusal'
  }
}
